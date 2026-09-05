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

"""Studio 写操作领域层。

所有写操作走**同一条写路径**：storage.chapter_path()（mysql 模式先 rehydrate）
→ Project.load → 领域模块（review/versioning）变更 → Project.save()
（本地文件 + notify_saved 钩子自动 upsert 入库）——与 CLI/引擎完全一致，
Web 端与命令行永远看到同一份状态。

写并发协调分三档：表态类端点走 `_mutate`（磁盘现状为基线），产物移动类动作走
`_exclusive`（章节操作锁内装载，正在生成的章节直接拒绝），其余低频编辑沿用
「最后写入生效」+ 人类表态三方合并。
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .. import lineage, review
from ..errors import KinemaError
from ..pipeline import consistency, versioning
from ..project import Project
from ..storage import get_storage
from ..storage.snowflake import next_id


def _gate(ws_root: Path, pid: str) -> None:
    """章节级写路径的删态闸——与 CLI `_project_path` 走同一道 `Workspace.get_project`。

    storage 的 `load_chapter`/`chapter_path` 不查 `is_deleted`，只靠它们装载等于
    绕过总闸：软删项目的章节仍可经 `/api/review`、`/api/rollback` 等端点写入，
    而前端 `.ro-deleted` 只拦得住页面上的点击，拦不住 API 调用。"""
    from ..workspace import Workspace
    Workspace(ws_root).get_project(pid)


def _load(ws_root: Path, pid: str, cid: str) -> Project:
    _gate(ws_root, pid)
    store = get_storage(ws_root)
    if store.load_chapter(pid, cid) is None:
        raise KinemaError(f"找不到章节: {pid}/{cid}")
    return Project.load(store.chapter_path(pid, cid))


def _mutate(ws_root: Path, pid: str, cid: str, fn):
    """表态类端点的写入口：锁内以磁盘最新内容应用变更（Project.mutate）。

    引擎长任务运行期间这些端点随时会被点击——端点持整份内存副本再 save
    会把引擎刚回填的产物字段写回旧值，表态必须基于磁盘现状。"""
    _gate(ws_root, pid)
    store = get_storage(ws_root)
    if store.load_chapter(pid, cid) is None:
        raise KinemaError(f"找不到章节: {pid}/{cid}")
    return Project.mutate(store.chapter_path(pid, cid), fn)


@contextmanager
def _exclusive(ws_root: Path, pid: str, cid: str, kind: str):
    """移动产物或改写非表态字段的写入口：章节操作锁内装载，与生成/合成任务同闸。

    回滚、段切换会移动画布文件；转场、垫图、特效、水印、字幕样式、previz 编排
    改的是 `Project.save` 合并面之外的键——与正在跑的任务交错时，任务收尾写盘会把
    这些编辑写回旧值，文件移动更无从合并。锁必须先于装载，装载后再锁拿到的仍是
    可能过期的副本。派子进程的动作要在块外派：子进程自己也要过这把锁。"""
    from ..locking import op_lock
    _gate(ws_root, pid)
    store = get_storage(ws_root)
    if store.load_chapter(pid, cid) is None:
        raise KinemaError(f"找不到章节: {pid}/{cid}")
    path = Path(store.chapter_path(pid, cid))
    with op_lock(path, kind=kind):
        yield Project.load(path)


def _find_shot(project: Project, shot_no) -> dict:
    s = next((x for x in project.shots if str(x.get("id")) == str(shot_no)), None)
    if s is None:
        raise KinemaError(f"找不到镜 {shot_no}")
    return s


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ---- 表态（两键表态的后端）----
def approve_shot_prompt(ws_root, pid, cid, *, shot, sha=None) -> dict:
    """实发稿审阅锁：记录/撤销本镜视频提示词的通过快照（sha 空即撤销）。

    只存正文 sha——gen-video 真发前重编译比对，一致才发；比对口径与
    `cli._prompt_sha` 同一份（Envelope fingerprint 含 references，锚定音预热
    会动附件清单而正文不变，按它比对会误报失效）。sha 由页面从预览行取，
    即用户刚看过的那份编译产物。"""
    def fn(project):
        s = _find_shot(project, shot)
        gen = s.setdefault("gen", {})
        if sha:
            gen["clip_approval"] = {"sha": str(sha), "at": _now()}
        else:
            gen.pop("clip_approval", None)
        return {"ok": True, "shot": s.get("id"),
                "approval": gen.get("clip_approval")}
    return _mutate(ws_root, pid, cid, fn)


def set_review(ws_root, pid, cid, *, shots=None, stage, state, note=None) -> dict:
    stage = stage or ("shot" if state == "omt" else None)
    if not stage:
        raise KinemaError("缺少 stage（image/audio/clip；弃用整镜用 state=omt）")
    if stage in review.CHAPTER_STAGES:     # 章节级产物（animatic 全片样片）表态
        def _chapter(project):
            review.set_state(project.data, stage, state, note=note)
            return {"updated": 1, "stage": stage, "state": state}
        return _mutate(ws_root, pid, cid, _chapter)
    if shots is None:
        raise KinemaError("缺少 shots（镜号列表）")
    nos = [str(x) for x in (shots if isinstance(shots, list) else [shots])]

    def _shots(project):
        for no in nos:
            review.set_state(_find_shot(project, no), stage, state, note=note)
        return {"updated": len(nos), "stage": stage, "state": state}
    return _mutate(ws_root, pid, cid, _shots)


# ---- 锚定评论：像素锚(x,y ∈ 0~1) / 时间锚(t 秒) ----
def _asset_comment_pool(series, kind, name):
    """定位设定图的提意见池（存系列文档内）：character/prop 存其条目 `comments`、
    scene 具名存 `scenes[]` 条目 `comments`、全局场景图存系列级 `scene_comments`。
    返回可 append 的 list（缺则建）。scene 的 name 分派与 `refine._asset_refs`
    同判据——两边多看或少看 name，具名取景地的意见就会串进全局池共用。"""
    if kind == "character":
        c = next((x for x in series.characters if x.get("name") == name), None)
        if not c:
            raise KinemaError(f"找不到角色 {name}")
        return c.setdefault("comments", [])
    if kind in ("prop", "weapon"):
        p = next((x for x in series.props if x.get("name") == name), None)
        if not p:
            raise KinemaError(f"找不到道具 {name}")
        return p.setdefault("comments", [])
    if kind in ("scene", "topview"):
        # 基准图与俯视图各有一池：共用一个的话，「墙的位置画错了」这类只对图纸成立的
        # 批注会被带进基准图的重生请求（`regen_asset` 把整池编译成指令）
        field = "comments" if kind == "scene" else "topview_comments"
        if name:
            sc = next((x for x in series.scenes if x.get("name") == name), None)
            if not sc:
                raise KinemaError(f"找不到取景地 {name}")
            return sc.setdefault(field, [])
        return series.data.setdefault(
            "scene_comments" if kind == "scene" else "scene_topview_comments", [])
    raise KinemaError(f"未知设定图类型: {kind}（character / scene / topview / prop）")


def add_comment(ws_root, pid, cid, *, shot=None, stage="image",
                text="", x=None, y=None, t=None, path=None,
                asset_kind=None, asset_name=None) -> dict:
    text = (text or "").strip()
    if not text:
        raise KinemaError("评论内容不能为空")
    entry = {"id": str(next_id()), "stage": stage, "text": text[:1000],
             "at": _now()}
    if x is not None:
        entry["x"] = round(float(x), 4)
    if y is not None:
        entry["y"] = round(float(y), 4)
    if t is not None:
        entry["t"] = round(float(t), 2)
    if path:   # 划线圈范围的笔迹（归一化点列，锚点 x/y 已取质心）——渲染层描线用
        entry["path"] = [[round(float(px), 3), round(float(py), 3)]
                         for px, py in list(path)[:200]]
    if asset_kind:        # 设定图提意见 → 存系列文档资产条目（无章节）
        from ..workspace import Workspace
        series = Workspace.open(str(ws_root), create=False).get_project(pid)
        # 同一页上的「重出设定图」派的是跑几分钟的子进程，写的是同一份系列文档
        with series.commit():
            _asset_comment_pool(series, asset_kind, asset_name).append(entry)
        return {"comment": entry}
    def _append(project):
        if shot is None:      # 章节级（成片 final）评论
            project.data.setdefault("comments", []).append(entry)
        else:
            _find_shot(project, shot).setdefault("comments", []).append(entry)
        return {"comment": entry}
    return _mutate(ws_root, pid, cid, _append)


def update_comment(ws_root, pid, cid, *, comment_id,
                   text=None, delete=False, asset_kind=None, asset_name=None) -> dict:
    """改造意见的改/删：text=改写内容（选中即改）；delete=删除。
    意见没有"已解决"中间态——改好经审核通过即整体消费删除。"""
    if asset_kind:        # 设定图提意见（系列文档）
        from ..workspace import Workspace
        series = Workspace.open(str(ws_root), create=False).get_project(pid)
        with series.commit():                  # 并发面同 add_comment
            pool = _asset_comment_pool(series, asset_kind, asset_name)
            hit = next((c for c in pool if str(c.get("id")) == str(comment_id)), None)
            if hit is None:
                raise KinemaError(f"找不到评论 {comment_id}")
            if delete:
                pool.remove(hit)
            elif text is not None and str(text).strip():
                hit["text"] = str(text).strip()[:1000]
        return {"comment_id": comment_id, "deleted": bool(delete)}
    def _edit(project):
        pools = [project.data.setdefault("comments", [])] + \
                [s.setdefault("comments", []) for s in project.shots]
        for pool in pools:
            for c in pool:
                if str(c.get("id")) == str(comment_id):
                    if delete:
                        pool.remove(c)
                    elif text is not None and str(text).strip():
                        c["text"] = str(text).strip()[:1000]
                    return {"comment_id": comment_id, "deleted": bool(delete)}
        raise KinemaError(f"找不到评论 {comment_id}")
    return _mutate(ws_root, pid, cid, _edit)


# ---- 导出中心：Studio 一键出提案/审阅包/交付包 ----
def export_artifact(ws_root, store, *, kind, pid, cid=None) -> dict:
    from ..workspace import Workspace
    ws = Workspace.open(str(ws_root), create=False)
    if kind == "pitch":
        from ..export import build_pitch_page
        index = build_pitch_page(ws, pid)
        return {"kind": kind, "path": str(index),
                "hint": "浏览器打开后「打印 → 存为 PDF」即得提案 PDF"}
    if cid is None:
        raise KinemaError(f"导出 {kind} 需要指定章节")
    project = _load(ws_root, pid, cid)
    if kind == "review":
        from ..export import build_review_page
        index = build_review_page(project, store,
                                  project.exports_dir / f"{project.path.stem}_review")
        return {"kind": kind, "path": str(index), "hint": "免登录，可直接发客户"}
    if kind == "deliver":
        from ..deliver import build_delivery
        from ..pipeline.subtitle import sub_cfg
        pdoc = ws.get_project(pid).data
        r = build_delivery(project, platforms=pdoc.get("platform"),
                           license_kind=pdoc.get("license"),
                           subtitle_lang=sub_cfg(store, project).get("lang"))
        return {"kind": kind, "path": r["zip"] or r["dir"],
                "hint": f"{len(r['platforms'])} 平台 · {r['files']} 个文件"}
    raise KinemaError(f"未知导出类型: {kind}（可选: pitch / review / deliver）")


# ---- 框选局部改造：分镜图 / 设定图，画布上"只改这一处" ----
def refine_image(ws_root, *, pid, cid=None, shot=None,
                 asset_kind=None, asset_name=None, rect=None,
                 instruction, mock=False) -> dict:
    from ..models import ModelRouter
    from ..refine import refine_asset, refine_shot_image
    from ..workspace import Workspace
    store = _fresh_store()
    router = ModelRouter(store, force_mock=bool(mock))
    if shot is not None:
        if not cid:
            raise KinemaError("分镜图改造需要指定章节")
        with _exclusive(ws_root, pid, cid, "refine") as project:
            return refine_shot_image(project, store, router, shot_no=shot,
                                     rect=rect, instruction=instruction)
    if asset_kind:
        ws = Workspace.open(str(ws_root), create=False)
        return refine_asset(ws, store, router, pid=pid, kind=asset_kind,
                            name=asset_name, rect=rect, instruction=instruction)
    raise KinemaError("要么给 shot（分镜图），要么给 asset_kind（设定图）")


def supply_shot_image(ws_root, pid, cid, *, shot, path, aspect=None,
                      skip_check=False) -> dict:
    """Studio 素材直供：path 限定工作区内（上传端点先落 assets/supply/ 再进这里）——
    与 voice clone 的 source 同一条安全边界，浏览器不可指向任意系统文件。

    **供料体检与 CLI 同一条闸**（在 supply_image 内部）：这里补上 ConfigStore
    作为画布基准，skip_check 对应网页「跳过体检」开关。"""
    from ..models import ConfigStore
    from ..supply import supply_image
    p = Path(path).expanduser().resolve()
    try:
        p.relative_to(Path(ws_root).resolve())
    except ValueError:
        raise KinemaError("素材路径必须位于工作区内（先用「⇪ 素材直供」上传）") from None
    with _exclusive(ws_root, pid, cid, "supply") as project:
        return supply_image(project, shot, p, aspect=aspect,
                            store=ConfigStore.shared(None), skip_check=bool(skip_check))


def delete_project(ws_root, pid) -> dict:
    """逻辑删除项目（唯一删除语义）：is_deleted=1 + deleted_at 落文档并同步库行，
    目录/产物/库数据完整保留——与 CLI `project rm` 同一条 Workspace 路径。"""
    from ..workspace import Workspace
    ws = Workspace.open(str(ws_root), create=False)
    ws.delete_project(pid)
    return {"project": pid, "deleted": True}


def restore_project(ws_root, pid) -> dict:
    """恢复逻辑删除的项目——与 CLI `project restore` 同一条路径。"""
    from ..workspace import Workspace
    ws = Workspace.open(str(ws_root), create=False)
    s = ws.restore_project(pid)
    return {"project": pid, "restored": True, "title": s.data.get("title")}


def create_project(ws_root, *, title, profile="narration", aspect=None,
                   platform=None, subtitle_lang=None, logline=None,
                   character=None, pid=None, skill=None) -> dict:
    """网页「＋ 新建项目」：与 CLI `project new` 同一条 Workspace.create_project 路径，
    只建**确定性空壳**（标题 / 画风 / 绑定 skill / 比例 / 字幕语言 + 立项即快照
    style_prompt 画风前缀），世界观 / 角色外貌 / 分镜等深度设定留给 AI 智能补全。
    skill 缺省由画风派生（可显式指定）；可选同步创建主角。"""
    from ..workspace import Workspace
    from ..models import ConfigStore
    title = (title or "").strip()
    if not title:
        raise KinemaError("项目标题不能为空")
    plats = None
    if isinstance(platform, str) and platform.strip():
        plats = [x.strip() for x in platform.split(",") if x.strip()]
    elif isinstance(platform, list) and platform:
        plats = platform
    ws = Workspace.open(str(ws_root), create=False)
    s = ws.create_project(title, profile=(profile or "narration"),
                          platform=plats, aspect=(aspect or None),
                          pid=(pid or None), skill=(skill or None))
    if subtitle_lang in ("zh", "en", "both"):
        s.data["subtitle_lang"] = subtitle_lang   # 章节创建时继承
    if logline and str(logline).strip():
        s.data.setdefault("design", {})["logline"] = str(logline).strip()
    # 画风单点落位（实体在 workspace.snapshot_style_prompt，CLI project new 同源）
    from ..workspace import snapshot_style_prompt
    try:
        store = ConfigStore.shared(None)
    except Exception:   # noqa: BLE001 —— 无配置不阻断建项目
        store = None
    snapshot_style_prompt(s, store)
    s.save()
    if character and str(character).strip():   # 可选：同步创建主角（深度外貌交给 AI）
        try:
            s.add_character(str(character).strip(), role="主角")
        except Exception:   # noqa: BLE001 —— 重名/异常不阻断建项目
            pass
    return {"project": s.pid, "title": s.data.get("title"),
            "profile": s.data.get("profile"), "skill": s.data.get("skill"),
            "created": True}


def _regen_note(s: dict) -> str:
    """重生 note 编译：镜上全部锚定意见自动汇入（带九宫格方位词）——
    钉点意见经驳回闭环直接进下一版提示词的「本次修正重点」，人不用再誊一遍；
    审核通过时意见整体消费删除（review.set_state），不会滚存到下一轮。"""
    rows, cols = ("上", "中", "下"), ("左", "中", "右")
    pending = [c for c in (s.get("comments") or [])
               if (c.get("text") or "").strip()
               and c.get("stage", "image") == "image"]
    parts = []
    for c in pending:
        pos = (f"（画面{rows[min(2, int(float(c['y']) * 3))]}"
               f"{cols[min(2, int(float(c['x']) * 3))]}）"
               if c.get("x") is not None and c.get("y") is not None else "")
        parts.append(f"{c['text'].strip()}{pos}")
    if not parts:
        return "Studio 重新生成"
    if len(parts) > 1:   # 多条意见带序号——每条都是必改项，防模型只挑第一条执行
        nums = "①②③④⑤⑥⑦⑧⑨⑩"
        parts = [f"{nums[i] if i < len(nums) else f'({i + 1})'} {p}"
                 for i, p in enumerate(parts)]
        return ("Studio 重新生成——按未解决批注逐条修正（共"
                f"{len(parts)}处，缺一不可）：" + "；".join(parts))
    return "Studio 重新生成——按未解决批注修正：" + parts[0]


def regen_shot(ws_root, pid, cid, *, shot, mock=False) -> dict:
    """Studio「↻ 重新生成」：置 retake（点按钮=人工决策，重生门会归档旧版）→
    后台任务跑 `gen-image --only <镜>`，前端凭 job_id 轮询、完成自动换图。
    未解决锚定评论自动编译进 retake note（见 _regen_note）。"""
    from . import jobs
    project = _load(ws_root, pid, cid)
    s = _find_shot(project, shot)
    if s.get("kind") == "transition":
        raise KinemaError("转场镜为纯本地渲染，无生成产物可重生")
    set_review(ws_root, pid, cid, shots=shot, stage="image", state="retake",
               note=_regen_note(s))
    args = ["gen-image", "--chapter", f"{pid}/{cid}", "--only", str(int(shot))]
    if mock:
        args.append("--mock")
    jid = jobs.spawn_cli(args, label=f"{pid}/{cid} 镜{shot} 重新生成",
                         ws_root=ws_root,
                         meta={"project": pid, "chapter": cid,
                               "shot": str(shot), "kind": "regen"})
    return {"job": jid}


def regen_asset(ws_root, pid, *, kind, name=None, mock=False) -> dict:
    """Studio 设定图「↻ 重新生成」：把该设定图上未解决的提意见编译成指令 → 后台跑
    `refine --id <pid> --asset <kind>[:<name>] --note <编译>`（全图应用批注、旧版备份、
    血缘传播下游分镜）→ 意见在任务**成功后**才消费（分镜侧是过审即消费；设定图
    无审阅环节，任务成功即视为已应用）。失败时批注原样保留——refine 因 API
    报错退出时 argv 不落盘、任务表随 Studio 重启即失，提交时清空等于永久丢失。"""
    from ..workspace import Workspace
    from . import jobs
    series = Workspace.open(str(ws_root), create=False).get_project(pid)
    pool = _asset_comment_pool(series, kind, name)
    note = _regen_note({"comments": pool})
    asset = f"{kind}:{name}" if name else kind
    if note == "Studio 重新生成":
        # **没提意见也要能重出**："没意见"恰恰是最常见的诉求——就是不满意、想按
        # **原本那套规则**再抽一张。故降级到整张重生
        # （`project refs --only <kind>[:名] --force`），它走的正是设定图的完整版式
        # 提示词（sheets 单一真源），比拿一句空指令去 refine 正确得多。
        args = ["project", "refs", pid, "--only", asset, "--force"]
        if mock:
            args.append("--mock")
        jid = jobs.spawn_cli(args, label=f"{pid} 设定图 {asset} 按原规则重出",
                             ws_root=ws_root,
                             meta={"project": pid, "kind": "regen_asset", "asset": asset})
        return {"job": jid, "mode": "fresh"}
    consumed = {c.get("id") for c in pool if c.get("id")}

    def _consume():
        # 按 id 删除而非整池清空：任务运行期间新提的意见不受波及。
        # 回调在 refine 子进程退出后才跑，与其对系列文档的写入不并发。
        s2 = Workspace.open(str(ws_root), create=False).get_project(pid)
        pool2 = _asset_comment_pool(s2, kind, name)
        pool2[:] = [c for c in pool2 if c.get("id") not in consumed]
        s2.save()

    args = ["refine", "--id", pid, "--asset", asset, "--note", note]
    if mock:
        args.append("--mock")
    jid = jobs.spawn_cli(args, label=f"{pid} 设定图 {asset} 重新生成", ws_root=ws_root,
                         meta={"project": pid, "kind": "regen_asset", "asset": asset},
                         on_success=_consume)
    return {"job": jid, "mode": "refine"}


def save_audio_script(ws_root, pid, cid, *, segments=None, mode=None) -> dict:
    """音频剧本台的写入口：整段剧本 + `audio_mode` 路线切换。

    剧本恒以 `{segments: [...]}` 落盘（哪怕只有一段）——单段写成裸字符串是
    `segment_script` 认的历史形态，但两种形态并存会让前端在「加一段」时要先猜
    当前是哪一种。段数与分段数对不对得上**交给引擎在生成时判**，这里不拦：
    正在写第一段的人本来就还没写完第二段，写一半存不下来是最难用的形态。

    `mode` 三态与工程既有口径一致：缺省=不动 · `tracks`=删除字段回落缺省 ·
    `scored`=切到音频剧本路线。"""
    if mode is not None and mode not in ("tracks", "scored"):
        raise KinemaError(f"audio_mode 只能是 tracks / scored：{mode}")

    def _write(project):
        if mode == "scored":
            project.data["audio_mode"] = "scored"
        elif mode == "tracks":
            project.data.pop("audio_mode", None)
        if segments is not None:
            rows = [str(x) for x in segments]
            if any(x.strip() for x in rows):
                project.data["audio_script"] = {"segments": rows}
            else:
                project.data.pop("audio_script", None)   # 全空=撤回，不留一份空壳
        return {"audio_mode": project.audio_mode,
                # 计的是**落盘条数**（含空段）：scanner 下发的 `written` 只数非空段，
                # 同名不同义会诱导前端误接，故这里用另一个名字
                "segments_saved":
                    len(((project.data.get("audio_script") or {}).get("segments")) or [])}
    return _mutate(ws_root, pid, cid, _write)


def draft_audio_script(ws_root, pid, cid) -> dict:
    """按分镜起草音频剧本（零成本·纯本地·不调任何模型）→ 逐段正文回给前端填框。

    **不落盘**：起草是给人看的初稿，让用户在框里改完再点存稿；直接写文档会把
    「我点一下看看」变成一次静默覆盖。CLI 的 `score --draft` 反过来是落盘的
    （命令行没有"框"这个中间态），两边的差异就在这一点上。"""
    from .. import audioscript
    project = _load(ws_root, pid, cid)
    rows, thin = audioscript.draft(project)
    return {"segments": rows, "thin": thin}


def switch_score_segment(ws_root, pid, cid, *, no, to_v) -> dict:
    """把某段切到历史版（零成本·纯本地互换 + 重拼整轨）。

    走 CLI 同一条路径的领域函数，不在网页另写一份切换逻辑——切完不重拼，
    盘上那条音轨还是旧的而页面已显示切过去了，是最难查的一类「改了没生效」。"""
    from ..audioscript import score_reconcat
    with _exclusive(ws_root, pid, cid, "score-switch") as project:
        versioning.rollback_score_segment(project, int(no), int(to_v))
        total = score_reconcat(project)
        return {"no": int(no), "to_v": int(to_v), "duration": round(total, 2)}


def score_generate(ws_root, pid, cid, *, only=None, force=False, mock=False) -> dict:
    """生成音频剧本整轨（后台任务，走 CLI 同一条路径——网页绝不另写生成逻辑）。

    `only`＝段号列表（不是镜号）：改了某一段剧本时只重生那一段，其余沿用盘上音轨。
    这条路按秒计费，所以**默认只补缺的段**，重生已在盘的段必须点名或 `force`。"""
    from . import jobs
    project = _load(ws_root, pid, cid)
    if not project.scored_audio:
        raise KinemaError("本章不是音频剧本路线——先在音频剧本台切到 scored")
    args = ["score", "--chapter", f"{pid}/{cid}"]
    sel = [str(x) for x in (only or []) if str(x).strip()]
    if sel:
        args += ["--only", ",".join(sel)]
    if force:
        args.append("--force")
    if mock:
        args.append("--mock")
    jid = jobs.spawn_cli(args, label=f"{pid}/{cid} 音频剧本", ws_root=ws_root,
                         meta={"project": pid, "chapter": cid, "kind": "score",
                               "segments": ",".join(sel)})
    return {"job": jid}


def set_effects(ws_root, pid, cid, *, effects, recompose=False, mock=False) -> dict:
    """Studio 特效选择器：写章节 json 顶层 effects（合成时唯一生效来源，语义
    真源见 models.effects_for）→ 可选后台重新合成。
    effects=None 删键、[] 留显式空表——合成结果一致（都不加特效）；
    未知特效名当场拒绝（静默过滤=前端 chip 画成生效、合成端却没有这层）。
    重合成走 assemble --draft（特效是过审后的修饰层，不该被合成审阅闸拦；
    成片本已存在，只是换滤镜重出）。"""
    from .. import effects as fx
    with _exclusive(ws_root, pid, cid, "effects") as project:
        if effects is None:
            project.data.pop("effects", None)
        else:
            unknown = [e for e in effects if e not in fx.EFFECTS]
            if unknown:
                raise KinemaError(f"未知特效名: {', '.join(str(x) for x in unknown)}"
                                  f"（可用: {', '.join(sorted(fx.EFFECTS))}）")
            project.data["effects"] = list(effects)
        project.save()
    job = None
    if recompose:
        from . import jobs
        args = ["assemble", "--chapter", f"{pid}/{cid}", "--draft"]
        if mock:
            args.append("--mock")
        job = jobs.spawn_cli(args, label=f"{pid}/{cid} 重新合成·特效", ws_root=ws_root,
                             meta={"project": pid, "chapter": cid, "kind": "compose"})
    return {"effects": project.data.get("effects"), "job": job}


# ---- 3D 导演控制台（previz）----
def previz_save(ws_root, pid, cid, *, scene) -> dict:
    """保存 3D 场景编排快照（章节文档顶层 `previz`）。

    走 `_load` → 领域模块 → `save()` 这条唯一写路径，和 CLI 完全同源；
    场景是**整体替换**（见 `previz.save_scene` 的说明：字段级合并会造出
    「引用了已删除机位的镜头块」这种半坏状态）。
    """
    from .. import previz as previz_mod
    with _exclusive(ws_root, pid, cid, "previz-save") as project:
        doc = previz_mod.save_scene(project, scene or {})
    return {"scene_hash": doc["scene_hash"], "updated_at": doc["updated_at"],
            "cuts": len(doc.get("cuts") or []), "actors": len(doc.get("actors") or [])}


def previz_render(ws_root, pid, cid, *, shot, fps=24, camera=None,
                  use_first_frame=None, mock=False) -> dict:
    """把已上传的帧序列编成 previz 并登记 —— 后台任务（`previz build` 子进程）。

    走子进程而非进程内直调：与 CLI 完全同一条路径，编码再慢也不占 HTTP 线程；
    `meta` 是忙态定位名片（分镜卡据此画「预演渲染中」遮罩，刷新页面也不丢）。
    """
    from . import jobs
    project = _load(ws_root, pid, cid)          # 章节不存在/镜号不合法先在这里炸
    _find_shot(project, shot)
    args = ["previz", "build", "--chapter", f"{pid}/{cid}", "--shot", str(shot),
            "--fps", str(int(fps or 24))]
    if camera:
        args += ["--camera", str(camera)]
    if use_first_frame is True:
        args.append("--use-first-frame")
    elif use_first_frame is False:
        args.append("--no-first-frame")
    jid = jobs.spawn_cli(args, label=f"{pid}/{cid} 镜{shot} 预演渲染", ws_root=ws_root,
                         meta={"project": pid, "chapter": cid,
                               "shot": str(shot), "kind": "previz"})
    return {"job": jid}


def previz_reel(ws_root, pid, cid) -> dict:
    """全片预演：各镜 previz → 一条长片（后台任务，`previz reel` 子进程）。

    走子进程与 CLI 同源；**在这里先算一遍可入片的镜**，一镜都没有就当场报错，
    别派一个注定失败的任务出去让用户等着看 job 尾巴。零 API 成本（纯本地 ffmpeg），
    故不经任何成本闸——它既不生成也不发送任何东西。
    """
    from . import jobs
    from .. import previz as previz_mod
    project = _load(ws_root, pid, cid)
    rows, _ = previz_mod.reel_inputs(project)
    if not rows:
        raise KinemaError("本章还没有任何镜渲出 previz，合不出全片预演——"
                             "先逐镜「⏺ 渲染 previz」")
    jid = jobs.spawn_cli(["previz", "reel", "--chapter", f"{pid}/{cid}"],
                         label=f"{pid}/{cid} 全片预演（{len(rows)} 镜）", ws_root=ws_root,
                         meta={"project": pid, "chapter": cid, "kind": "previz_reel"})
    return {"job": jid, "shots": len(rows)}


def previz_clear(ws_root, pid, cid, *, shot) -> dict:
    """摘除某镜的 previz 挂载（保留产物文件，不动分镜图）。"""
    from .. import previz as previz_mod
    with _exclusive(ws_root, pid, cid, "previz-clear") as project:
        return previz_mod.clear_previz(project, shot)


def previz_set_v2v(ws_root, pid, cid, *, on) -> dict:
    """项目级参考视频 V2V 开关（章节文档顶层 `previz_v2v`）。

    **这是个花钱开关**：开启后每次 gen-video 会把 previz 作参考视频发出，
    按 token 计费且**输入视频秒同样入账**。前端务必在开关旁写明这一点。
    """
    with _exclusive(ws_root, pid, cid, "previz-v2v") as project:
        if bool(project.data.get("previz_v2v")) != bool(on):
            locked = review.chapter_locked(project.shots, {"previz_v2v"})
            if locked:
                raise KinemaError(
                    f"章节已有 {'/'.join(locked)} 通过锁定，V2V 开关改变请求形态——"
                    "要重生置 retake，只解锁不重生置 wfa")
        if on:
            project.data["previz_v2v"] = True
        else:
            project.data.pop("previz_v2v", None)
        project.save()
    return {"previz_v2v": bool(project.data.get("previz_v2v"))}


def previz_to_seedance(ws_root, pid, cid, *, only=None, mock=False) -> dict:
    """「交给 Seedance」：以 native + V2V 出片（后台任务）。

    `only`＝导演在选镜弹层里勾的镜号列表——**不是每一镜都值得花 previz 的钱**，
    复杂调度镜带参考视频精确控制、简单镜留给常规流程；缺省（空）为全部正镜。
    刻意**不自动过审、不自动改 motion 落盘**——`gen-video -m b` 只在本次进程内
    覆盖模式（`_apply_aspect_args`），章节文档的 motion 不动；要不要长期切 native
    是导演的决定，不是一个按钮该替他做的。
    """
    from . import jobs
    project = _load(ws_root, pid, cid)
    if not any(s.get("previz") for s in project.shots):
        raise KinemaError("本章还没有任何镜登记 previz——先在 3D 导演控制台排戏并渲染")
    sel = [str(x) for x in (only or []) if str(x).strip()]
    # 只点名有 previz 的镜：meta.shots 驱动分镜卡「生成中」遮罩，把 gen-video
    # 不会处理的镜（未登记 previz/转场镜）算进去会点亮永远不会完成的忙态
    picked = [s for s in project.shots
              if s.get("previz") and (not sel or str(s.get("id")) in sel)]
    if sel and not picked:
        raise KinemaError("勾选的镜号没有已登记的 previz（或镜号不存在）")
    args = ["gen-video", "--chapter", f"{pid}/{cid}", "-m", "b", "--previz"]
    if sel:
        args += ["--only", ",".join(sel)]
    if mock:
        args.append("--mock")
    # meta.shots 带镜号清单（与 sketch 任务同款）：任务是章节级一条，制作台分镜卡的
    # 逐镜「生成中」遮罩与刷新后的忙态恢复都靠它——没有它，这个数分钟的花钱任务
    # 在章节视图里毫无痕迹
    jid = jobs.spawn_cli(args, label=f"{pid}/{cid} 交给 Seedance（V2V）", ws_root=ws_root,
                         meta={"project": pid, "chapter": cid, "kind": "previz_v2v",
                               "shots": ",".join(str(s.get("id")) for s in picked)})
    return {"job": jid, "shots": len(picked)}


# ---- 深度捕捉（control，与 previz / 简笔板逐镜互斥）----
def control_build(ws_root, pid, cid, *, source, asset=None, mock=False,
                  bind_shot=None) -> dict:
    """把一段源片处理成控制视频素材（后台任务）。

    **本函数不落 `_mutate`/`_exclusive` 任何一档**，因为 `control build` 全程
    不 load/save 章节文档：素材只写自己的目录。它跑几分钟，取章节操作锁会把
    gen-image/tts/assemble 一起堵死。互斥另在 `control/` 目录内的 build 锁上
    （跨进程，CLI 与网页共用一把）。
    """
    from . import jobs
    _gate(ws_root, pid)
    args = ["control", "build", "--chapter", f"{pid}/{cid}", "--source", str(source)]
    if asset:
        args += ["--asset", asset]
    if mock:
        args.append("--mock")
    # 上传时就点了镜的话，绑定跟着处理走：跑几分钟的活结束后人多半已经离开页面，
    # 让他回来再点一次绑定是白等一趟
    if bind_shot:
        args += ["--bind-shot", str(bind_shot)]
    jid = jobs.spawn_cli(args, label=f"{pid}/{cid} 深度捕捉", ws_root=ws_root,
                         meta={"project": pid, "chapter": cid,
                               "kind": "control_build", "asset": asset or "",
                               "bind_shot": str(bind_shot or "")})
    return {"job": jid}


def control_bind(ws_root, pid, cid, *, shot, asset, start=0.0, end=None,
                 fit="pad", replace_previz=False) -> dict:
    """把素材的 `[start, end)` 一段绑到某镜（同步裁段，约两秒）。

    `end` 省略时段长由该镜的请求秒数定；给了则区间说了算，并把 `dur` 对齐过去。
    """
    from .. import control as control_mod
    from ..models import ConfigStore
    with _exclusive(ws_root, pid, cid, "control-bind") as project:
        return control_mod.bind_shot(project, shot, asset, start=float(start),
                                     end=None if end is None else float(end),
                                     fit=fit, replace_previz=bool(replace_previz),
                                     store=ConfigStore.shared(None))


def control_compare(ws_root, pid, cid, *, shot) -> dict:
    """出某镜的三合一对照片（源片段 | 控制段 | 成片段）。

    **不落任何一档锁**：只读章节文档、只写自己的对照产物。转码几秒钟，占章节
    操作锁会把同章的生成一起堵住。
    """
    from .. import control as control_mod
    project = _load(ws_root, pid, cid)
    s = next((x for x in project.shots if str(x.get("id")) == str(shot)), None)
    if s is None:
        raise KinemaError(f"找不到镜 {shot}")
    dst = control_mod.build_shot_compare(project, s)
    from .scanner import _murl
    return {"shot": s["id"], "compare": _murl(str(dst))}


def control_unbind(ws_root, pid, cid, *, shot) -> dict:
    from .. import control as control_mod
    with _exclusive(ws_root, pid, cid, "control-unbind") as project:
        return control_mod.unbind_shot(project, shot)


def control_delete(ws_root, pid, cid, *, asset) -> dict:
    """删素材目录。仍有镜绑着即拒——判据要读章节文档，故同样进 `_exclusive`。"""
    from .. import control as control_mod
    with _exclusive(ws_root, pid, cid, "control-delete") as project:
        return control_mod.delete_asset(project, asset)


def control_set_v2v(ws_root, pid, cid, *, on) -> dict:
    """章级深度捕捉开关（章节文档顶层 `control_video`）。

    **这是个花钱开关**：开启后每次 gen-video 把控制视频作参考视频发出，
    按 token 计费且**输入视频秒同样入账**。前端务必在开关旁写明这一点。
    """
    with _exclusive(ws_root, pid, cid, "control-v2v") as project:
        if bool(project.data.get("control_video")) != bool(on):
            locked = review.chapter_locked(project.shots, {"control_video"})
            if locked:
                ids = [str(s.get("id")) for s in project.shots
                       if review.is_locked(s, "clip")]
                raise KinemaError(
                    f"镜 {'、'.join(ids)} 的{'/'.join(locked)}已通过锁定，"
                    "开关改变请求形态——要重生置 retake"
                    "（review set --stage clip --state retake），"
                    "只解锁不重生置 wfa")
        if on:
            project.data["control_video"] = True
        else:
            project.data.pop("control_video", None)
        project.save()
    return {"control_video": bool(project.data.get("control_video"))}


def control_to_seedance(ws_root, pid, cid, *, only=None, mock=False) -> dict:
    """「送 Seedance」：以 native + 深度控制视频出片（后台任务）。"""
    from . import jobs
    project = _load(ws_root, pid, cid)
    if not any(s.get("control") for s in project.shots):
        raise KinemaError("本章还没有任何镜绑定控制视频——先在深度控制台处理素材并绑定")
    sel = [str(x) for x in (only or []) if str(x).strip()]
    picked = [s for s in project.shots
              if s.get("control") and (not sel or str(s.get("id")) in sel)]
    if sel and not picked:
        raise KinemaError("勾选的镜号没有绑定控制视频（或镜号不存在）")
    args = ["gen-video", "--chapter", f"{pid}/{cid}", "-m", "b", "--control"]
    if sel:
        args += ["--only", ",".join(sel)]
    if mock:
        args.append("--mock")
    jid = jobs.spawn_cli(args, label=f"{pid}/{cid} 送 Seedance（深度）", ws_root=ws_root,
                         meta={"project": pid, "chapter": cid, "kind": "control_v2v",
                               "shots": ",".join(str(s.get("id")) for s in picked)})
    return {"job": jid, "shots": len(picked)}


# ---- 简笔分镜台（sketchboard，与 previz 并行互斥）----
def sketch_generate(ws_root, pid, cid, *, shots=None, force=False, mock=False) -> dict:
    """按 beats 逐镜生成简笔板（后台任务，走 CLI 同一条路径——网页绝不另写生成逻辑）。

    `shots`＝选镜弹层勾的镜号列表（缺省全部有 beats 的正镜）；缺 beats 的镜由
    CLI 侧点名跳过，前端复制「AI 分镜板指令」交指挥层补写。"""
    from .. import sketchboard as sketch_mod
    from . import jobs
    project = _load(ws_root, pid, cid)
    sel = [str(x) for x in (shots or []) if str(x).strip()]
    if not sel and not any(sketch_mod.effective_beats(s)[0] for s in project.shots):
        raise KinemaError("本章没有任何可拆拍的镜（运动设计全空）——先补分镜的"
                             "video_prompt/action，或按 kinema-sketchboard 写 beats")
    args = ["sketch", "gen", "--chapter", f"{pid}/{cid}"]
    if sel:
        args += ["--only", ",".join(sel)]
    if force:
        args.append("--force")
    if mock:
        args.append("--mock")
    # meta.shots 带镜号清单：任务是章节级一条，前端板条的逐镜「生成中」格
    # 与刷新后的忙态恢复都靠它（缺省=全部拆拍就绪的正镜）
    if not sel:
        sel = [str(s.get("id")) for s in project.shots
               if sketch_mod.effective_beats(s)[0]]
    jid = jobs.spawn_cli(args, label=f"{pid}/{cid} 简笔分镜板", ws_root=ws_root,
                         meta={"project": pid, "chapter": cid, "kind": "sketch",
                               "shots": ",".join(sel)})
    return {"job": jid, "shots": len(sel)}


def sketch_regen(ws_root, pid, cid, *, shot, note=None, mock=False) -> dict:
    """灯箱「重新生成」：单镜整板重出（--force），用户意见经 `--note` 编译进
    板提示词的「修正重点」——模版垫图 + 原拍序列 + 新要求三者合并，走 CLI 同一条
    生成路径（网页绝不另写拼装）。"""
    from . import jobs
    _load(ws_root, pid, cid)   # 校验章节存在（不存在早失败，别把错误留给子进程）
    args = ["sketch", "gen", "--chapter", f"{pid}/{cid}",
            "--only", str(int(shot)), "--force"]
    n = str(note or "").strip()
    if n:
        args += ["--note", n]
    if mock:
        args.append("--mock")
    jid = jobs.spawn_cli(args, label=f"{pid}/{cid} 镜{shot} 简笔板重生", ws_root=ws_root,
                         meta={"project": pid, "chapter": cid, "kind": "sketch",
                               "shots": str(int(shot))})
    return {"job": jid}


def sketch_guide(ws_root, pid, cid, *, shot, guide) -> dict:
    """逐镜表态运动预演路径（previz/control/sketch，auto 清除表态）——`shots[].guide`
    的网页写入口。合法值取 `sketchboard.GUIDES`，与 CLI `sketch use` 同一张表。

    `guide` 是长任务期间的人类表态（`_SHOT_HUMAN_KEYS` 登记项），走 `_mutate`
    以磁盘现状为基线。"""
    from .. import sketchboard as sketch_mod
    g = str(guide or "").strip().lower()
    if g not in (*sketch_mod.GUIDES, "auto"):
        raise KinemaError(f"guide 只认 {' / '.join(sketch_mod.GUIDES)} / auto")

    def _set(project):
        s = _find_shot(project, shot)
        if g == "auto":
            s.pop("guide", None)
        else:
            s["guide"] = g
        return {"shot": s.get("id"), "guide": s.get("guide"),
                "active": sketch_mod.active_guide(s)}
    return _mutate(ws_root, pid, cid, _set)


def sketch_clear(ws_root, pid, cid, *, shot) -> dict:
    """摘除某镜的简笔板挂载（板文件保留，beats 不动）。"""
    from .. import sketchboard as sketch_mod
    with _exclusive(ws_root, pid, cid, "sketch-clear") as project:
        return sketch_mod.clear_board(project, int(shot))


# ---- 音色选角：试音 → 立档 → 启用（单一真源 voicebank）----
def _series(ws_root, pid):
    from ..workspace import Workspace
    return Workspace.open(str(ws_root), create=False).get_project(pid)


def _audition_view(entries: list[dict]) -> list[dict]:
    """候选下发口径：**带可播放地址与入档归属**。只回编号的话前端拿不到音频，
    就地插入只能渲染成占位文字，于是又得整页重绘去换 URL——重绘会丢失
    当前的滚动位置与操作状态。`cast` 必须随行：就地重绘的候选行不再经过 bank_view，
    丢了它，已入档的官方音色在新批次里会重新显示「选定」钮。
    URL 与 scanner 同一个 `media_url`。"""
    from .scanner import _murl
    return [{"no": e.get("no"), "voice": e.get("voice"),
             "voice_type": e.get("voice_type"), "cast": e.get("cast"),
             "media": _murl(e.get("path"))} for e in entries]


def voice_audition(ws_root, pid, *, owner, candidates=None, mock=False) -> dict:
    from ..models import ModelRouter
    from ..voicebank import audition, bank_view
    store = _fresh_store()
    router = ModelRouter(store, force_mock=bool(mock))
    cands = [c for c in (candidates or []) if c] or None
    series = _series(ws_root, pid)
    r = audition(store, router, series, owner, candidates=cands)
    # 入档归属取 bank_view 同一真源，不在这里另判一遍
    view = bank_view(series, owner)
    return {"owner": r["owner"], "batch": r["batch"],
            "entries": _audition_view(view["audition"]["entries"])}


def voice_custom(ws_root, pid, *, owner, prompt, count=None, mock=False) -> dict:
    from ..models import ModelRouter
    from ..voicebank import CUSTOM_COUNT, bank_view, custom_audition
    store = _fresh_store()
    router = ModelRouter(store, force_mock=bool(mock))
    series = _series(ws_root, pid)
    r = custom_audition(store, router, series, owner,
                        prompt=prompt, count=int(count or CUSTOM_COUNT))
    view = bank_view(series, owner)
    return {"owner": r["owner"], "batch": r["batch"], "prompt": r["prompt"],
            "entries": _audition_view(view["custom_audition"]["entries"])}


def voice_use(ws_root, pid, *, owner=None, no=None, custom=False, cast=None,
              mock=False) -> dict:
    """启用一把声音：候选按编号立档，或 `cast` 换回档案里已有的一条。

    带 router 调用：启用即预热锚定参考音，章节页的「参考音频N」当场可试听——
    听得到音色才谈得上决定要不要开按秒计费的生视频。"""
    from ..models import ModelRouter
    from ..voicebank import use_audition, use_cast, use_custom
    series, store = _series(ws_root, pid), _fresh_store()
    router = ModelRouter(store, force_mock=bool(mock))
    if cast:
        return use_cast(series, store, str(cast), router=router)
    if not owner or not no:
        raise KinemaError("启用音色要给 owner + no（候选编号），或给 cast（档案号）")
    return (use_custom(series, store, owner, int(no), router=router) if custom
            else use_audition(series, store, owner, int(no), router=router))


def voice_anchor_warm(ws_root, pid, cid, *, shot, no, mock=False) -> dict:
    """按需预热某条锚定参考音 → `{who, no, media}`。

    入参是「哪一镜的第几条参考音」而不是音色 ID：编号→说话人→音色的映射走
    `voicecast.voice_anchor_plan` 同一份计划，与页面注记、dry-run 预览、真发
    三处同源。前端报一个音色名过来的话，它与实发点名的那把就有分叉余地。

    让人在按秒计费之前听准嗓音：模版音色的锚定音未落盘时由此补合成，缓存按音色
    命名，真发直接命中。"""
    from .. import voicecast
    from ..models import ModelRouter
    from ..voicebank import ensure_anchor_clip
    from .scanner import _murl
    project = _load(ws_root, pid, cid)
    s = _find_shot(project, int(shot))
    store = _fresh_store()
    series = _series(ws_root, pid)
    # 参考位限额取真发那一档（与 scanner 卡片同源），否则高限额型号的第 4 个说话人
    # 在卡片上有编号、点试听却被判「没有这条参考音」
    from ..models import resolve_video
    vprov = resolve_video(ModelRouter(store), store, project.data, project.data.get("profile"))[0]
    plan = voicecast.voice_anchor_plan(
        project, store, s,
        max_refs=int(getattr(vprov, "max_ref_audios", 0) or 0) or voicecast.MAX_ANCHOR_REFS)
    hit = next((a for a in plan["anchored"] if int(a["no"]) == int(no)), None)
    if hit is None:
        raise KinemaError(f"镜 {shot} 没有第 {no} 条锚定参考音——先在项目页给该说话人选角")
    clip = ensure_anchor_clip(series, ModelRouter(store, force_mock=bool(mock)),
                              hit["voice_type"])
    if not clip:
        raise KinemaError(f"「{hit['who']}」的锚定音合成后仍不在盘——先跑 doctor 查 TTS 配置")
    return {"who": hit["who"], "no": int(no), "media": _murl(clip)}


def voice_delete(ws_root, pid, *, cast) -> dict:
    from ..voicebank import delete_cast
    if not cast:
        raise KinemaError("要删除哪条音色档案（cast）")
    return delete_cast(_series(ws_root, pid), str(cast))


# ---- 设定图候选点选（宫格定稿，与 CLI project pick-ref 同一条路）----
def pick_ref(ws_root, pid, *, kind, name=None, no) -> dict:
    from ..refine import pick_asset_candidate
    from ..workspace import Workspace
    ws = Workspace.open(str(ws_root), create=False)
    return pick_asset_candidate(ws, pid, kind=kind, name=name, no=int(no))


# ---- 宫格候选点选：Studio 宫格点一下 = CLI `pick` ----
def pick_image(ws_root, pid, cid, *, shot, no, keep_open=False) -> dict:
    from ..pipeline import candidates
    with _exclusive(ws_root, pid, cid, "pick") as project:
        s = _find_shot(project, shot)
        return candidates.pick(project, s, int(no), approve=not keep_open)


# ---- 版本回滚（面板的后端）----
def rollback_version(ws_root, pid, cid, *, shot, stage, to) -> dict:
    with _exclusive(ws_root, pid, cid, "rollback") as project:
        s = _find_shot(project, shot)
        versioning.rollback(project, s, stage, int(to))
        # 与 CLI 回滚同一纪律：配音回滚重探时长，片段回滚不动 dur（那是已为
        # 当前时间轴买下的画面秒数，换版不改变买了多少秒）
        if stage == "audio":
            from ..ffmpeg import probe_duration
            main = s.get("audio_file")
            if main and Path(main).is_file():
                s["dur"] = round(probe_duration(main), 2)
        consistency.invalidate(s, stage)   # 画布内容换成历史版 → 旧一致性判定作废（audio 空操作）
        clip = lineage.retake_clip_for_image(s) if stage == "image" else None
        review.set_state(s, stage, "wfa", note=f"回滚至 v{to}")
        project.save()
        return {"shot": shot, "stage": stage, "now_contains": f"v{to}",
                "current_version": versioning.current_version(s, stage), "clip": clip}


def rollback_output_version(ws_root, pid, cid, *, aspect, to) -> dict:
    """成片回滚：把某一比例的历史成片拷回标准输出路径（当前版先归档）。

    与分镜 `rollback_version` 对称，差别只在成片落章节文档顶层、按比例分谱系、且没有
    审阅阶段可置位。**不动水印版**（`output_wm`）——它是从某一版成片派生的交付物，
    回滚后要重新打，否则水印版与成片版对不上而两者都还挂在页面上。
    """
    with _exclusive(ws_root, pid, cid, "rollback-output") as project:
        std = versioning.rollback_output(project, aspect, int(to))
        (project.data.get("output_wm") or {}).pop(aspect, None)
        project.save()
        return {"aspect": aspect, "now_contains": f"v{to}", "file": std,
                "current_version": versioning.output_current_version(project, aspect)}


# ---- 转场镜：时间线上插拔字卡/素材转场 ----
def transition_add(ws_root, pid, cid, *, after, ttype=None, text=None,
                   asset=None, dur=None, edge=None,
                   direction=None, color=None, sound=None) -> dict:
    from ..pipeline import transitions as tr
    with _exclusive(ws_root, pid, cid, "transition") as project:
        shots = project.shots
        ids = [s.get("id") for s in shots]
        if not any(str(x) == str(after) for x in ids):
            raise KinemaError(f"找不到镜 {after}")
        nid = max((int(x) for x in ids if str(x).isdigit()), default=0) + 1
        # 缺省类型智能选：素材=clip｜有字=fade_black(总~1s)｜无字=fade 极简黑场(总~0.5s)；
        # 显式选了类型即照办（Studio 类型选择器传 ttype）。方向/主色/音效仅在非空时写入，
        # 交给 spec_of 归一化（非法值自动回落），与 CLI transition add 同制。
        kind = tr.pick_type(text, asset, ttype)
        spec = {"type": kind}
        if text and str(text).strip():
            spec["text"] = str(text).strip()
        if asset:
            spec["asset"] = asset
        if edge is not None:
            spec["edge"] = float(edge)
        for key, val in (("direction", direction), ("color", color), ("sound", sound)):
            if val:
                spec[key] = str(val).strip()
        shot = {"id": nid, "kind": "transition",
                "dur": tr.resolve_dur(kind, dur), "narration": "",
                "transition": spec}
        idx = next(i for i, x in enumerate(ids) if str(x) == str(after))
        shots.insert(idx + 1, shot)
        project.save()
    return {"ok": True, "id": nid, "after": after, "spec": spec}


def transition_remove(ws_root, pid, cid, *, shot) -> dict:
    from ..pipeline import transitions as tr
    with _exclusive(ws_root, pid, cid, "transition") as project:
        s = _find_shot(project, shot)
        if not tr.is_transition(s):
            raise KinemaError("只能移除转场镜——普通镜绝不删除（弃用走 omt）")
        project.shots.remove(s)
        project.save()
    return {"ok": True, "removed": shot}


# ---- 剧本改编：据分集大纲幂等建章（系列文档写路径，机械承接·零 LLM）----
def adapt_scaffold(ws_root, pid, *, only=None) -> dict:
    """据系列 episodes[] 幂等批量建章 + 拷 outline（章号==集号，可重跑不炸）。

    与 CLI `adapt scaffold` 同一条写路径（Workspace.get_project → Series →
    save）。only 可为集号列表或逗号串，限定只建这几集。
    """
    from ..workspace import Workspace
    series = Workspace.open(str(ws_root), create=False).get_project(pid)
    if not series.episodes:
        raise KinemaError("尚无分集大纲（episodes[]）——请先由指挥层拆书分集")
    onlylist = None
    if only:
        seq = only if isinstance(only, list) else str(only).split(",")
        try:
            onlylist = [int(str(x).strip()) for x in seq if str(x).strip()]
        except ValueError as e:
            raise KinemaError(f"only 需为集号（整数）：{only}") from e
    res = series.scaffold_episodes(only=onlylist)
    series.sync_design_to_chapters()   # 设定集回灌已建章节（与 project refs 同制度）
    return {"scaffold": res}


def novel_thread(ws_root, pid, *, tid, status, paid_in=None, note=None) -> dict:
    """伏笔账本状态标记（网页创作 Tab「回收/弃置」按钮）——与 CLI
    `novel thread-pay/-drop` 同一条写路径（novel.thread_mark，经 Series.commit）。
    只做记账不做创作：新伏笔登记/正文/大纲仍走「复制指令给 Claude Code」范式。"""
    from ..workspace import Workspace
    from .. import novel as N
    series = Workspace.open(str(ws_root), create=False).get_project(pid)
    if not tid or status not in ("paid", "dropped", "open"):
        raise KinemaError("需要 tid 与 status（paid/dropped/open）")
    if status == "paid" and paid_in is None:
        raise KinemaError("标记回收必须给 paid_in 回收章号")
    t = N.thread_mark(series, str(tid), status=status,
                      paid_in=int(paid_in) if paid_in is not None else None,
                      note=note)
    return {"thread": t}


def clear_source(ws_root, pid) -> dict:
    """清空源文本资源（raw.txt + 结构目录 segments.json + 系列 `source` 指针），
    把项目退回「未入库」的剧本创作初期态——供换书/推倒重来。

    **硬闸（逻辑准确）**：一旦项目**已建章节**（`chapters[]` 非空，即已进入制作期，
    下游 outline/分镜/成片会与源文本产生血缘）就**拒绝清空**——「还没生成章节、
    还在剧本创作期间」才允许。已出片（章节 work/output 有成片）作为更强拦截理由
    并入报错文案。拆书/分集草稿（adaptation/episodes）不属「源资源」，此处不动。
    """
    from ..workspace import Workspace
    series = Workspace.open(str(ws_root), create=False).get_project(pid)
    chapters = series.data.get("chapters") or []
    if chapters:
        published = 0
        for ch in chapters:
            outdir = Path(ws_root) / pid / "chapters" / f"{ch.get('id')}_work" / "output"
            if outdir.is_dir() and any(outdir.glob("*.mp4")):
                published += 1
        detail = f"已建 {len(chapters)} 章" + (f"、其中 {published} 章已出片" if published else "")
        raise KinemaError(
            f"{detail}，项目已进入制作期，无法清空源文本。"
            "清空仅限剧本创作初期（尚未建章）；如确需推倒重来，请先删除章节或直接删除项目。")
    removed = []
    srcdir = Path(ws_root) / pid / "source"
    for name in ("raw.txt", "segments.json"):
        f = srcdir / name
        if f.is_file():
            f.unlink()
            removed.append(name)
    if srcdir.is_dir() and not any(srcdir.iterdir()):   # 目录空了一并收掉
        srcdir.rmdir()
    series.data.pop("source", None)                     # 清指针（save 同步库行）
    series.save()
    return {"project": pid, "cleared": True, "removed": removed}


# ---- 动态水印（防搬运，成片后）----
def _wm_active(project: Project) -> tuple[bool, bool, bool]:
    """(漂移, 固定角标, 底部水印)——按 project 字段判断当前该烧哪些。"""
    fx = project.data.get("watermark_fixed") or {}
    bt = project.data.get("watermark_bottom") or {}
    return (bool((project.data.get("watermark") or "").strip()),
            bool((fx.get("text") or "").strip()),
            bool((bt.get("text") or "").strip()))


def _remove_wm_version(project: Project) -> int:
    """删水印版文件并清 `output_wm` 记录 → 删掉的文件数。

    「三类水印全空」时的唯一清理收口——`set_watermark` 即时烧与 `rebuild_final`
    延后重烧两条路共用：页面默认播水印版（有记录就优先选它），字段清了而
    水印版还在盘上，用户看到的就一直是旧水印旧字幕那条片。"""
    from ..storage.media import ensure_local
    removed = 0
    for _asp, p in (project.data.get("output_wm") or {}).items():
        try:
            fp = Path(ensure_local(p))
            if fp.is_file():
                fp.unlink()
                removed += 1
        except Exception:  # noqa: BLE001  某比例文件缺失不阻断其余清理
            pass
    project.data.pop("output_wm", None)
    project.save()
    return removed


def _refresh_watermark(ws_root, pid, cid, project: Project) -> dict:
    """任一水印字段变更后统一收口：
      · 三类水印都空 → **删除水印版**并清 output_wm；
      · 仍有任一水印 → 后台按 project 现状**从干净原片重烧** output_wm
        （`watermark --from-project --force`，不回落 branding，与 UI 状态严格一致）。
    从干净 output 起烧 = 关掉某一类后重烧就真的没有它（水印不会叠加累积）。"""
    floating, fixed, bottom = _wm_active(project)
    if not floating and not fixed and not bottom:   # 都空 = 删除水印版
        return {"watermarked": False, "removed": _remove_wm_version(project)}
    outputs = {a: p for a, p in (project.data.get("output") or {}).items()
               if isinstance(p, str) and p}
    if not outputs:
        raise KinemaError("没有成片可打水印——先合成出片后再加水印。")
    project.save()
    from . import jobs
    jid = jobs.spawn_cli(
        ["watermark", "--chapter", f"{pid}/{cid}", "--from-project", "--force"],
        label=f"水印 {pid}/{cid}", ws_root=ws_root,
        meta={"project": pid, "chapter": cid, "kind": "watermark"})
    return {"watermarked": True, "floating": floating, "fixed": fixed,
            "bottom": bottom, "job": jid}


def set_watermark(ws_root, pid, cid, *, text=None, fixed_text=None,
                  fixed_position=None, bottom_text=None, burn=True) -> dict:
    """水印设置**唯一写入口**（漂移 + 固定角标 + 底部水印一次写完、只重烧一次）。

    与 CLI `watermark` 同一条渲染路径。文案参数各自三态：
      · `None`  → 这一类**不动**（保持现状）；
      · `""`    → 清除这一类；
      · 非空串  → 记住文案（`project.watermark` / `watermark_fixed` / `watermark_bottom`）。
    `fixed_position` 仅在固定角标有文案时生效，取值 tl/tr/bl/br。

    **刻意合并成一个写入口**：三类水印烧的是同一份成片，分多次 POST 就会起多个
    `watermark --from-project --force` 任务同时改写同一批 `output_wm` 文件——
    后完成的那个会以其他类的旧状态为准，出现"刚设的角标又没了"这种见了鬼的现象。
    合并后无论改几类，都是一次写盘 + 一次重烧。

    `burn=False` 只写盘不烧：同一次提交里还要改字幕样式时，重烧归 `rebuild_final`
    一条链（合成 → 水印按序执行）——这里再起一个水印任务就是两个任务抢写同一批
    `output_wm`，正是本函数合并写入口要防的那类竞态。
    """
    with _exclusive(ws_root, pid, cid, "watermark-set") as project:
        if text is not None:
            t = text.strip()
            if t:
                project.data["watermark"] = t      # 记住文案（下次预填 + CLI 兜底）
            else:
                project.data.pop("watermark", None)
        if fixed_text is not None:
            ft = fixed_text.strip()
            if ft:
                fx = dict(project.data.get("watermark_fixed") or {})
                fx["text"] = ft
                if fixed_position in ("tl", "tr", "bl", "br"):
                    fx["position"] = fixed_position
                fx.setdefault("position", "br")
                project.data["watermark_fixed"] = fx
            else:
                project.data.pop("watermark_fixed", None)
        if bottom_text is not None:
            bt = bottom_text.strip()
            if bt:
                b = dict(project.data.get("watermark_bottom") or {})
                b["text"] = bt
                project.data["watermark_bottom"] = b
            else:
                project.data.pop("watermark_bottom", None)
        project.save()
    if not burn:
        floating, fixed, bottom = _wm_active(project)
        return {"watermarked": bool(floating or fixed or bottom),
                "floating": floating, "fixed": fixed, "bottom": bottom,
                "burned": False}
    return _refresh_watermark(ws_root, pid, cid, project)


# 字幕样式的可覆盖键白名单：与 `subtitle._CAPTION_DEFAULTS` 的样式面一致。
# 白名单是硬要求——subtitle 块还承载 lang/mode 等行为键，样式面板绝不该碰它们
# （改错一个 lang 整章字幕换语言，比样式难看严重得多）。
_SUBTITLE_STYLE_KEYS = ("size", "text_color", "outline_color", "outline",
                        "shadow", "bold", "margin_v")


def set_subtitle_style(ws_root, pid, cid, *, style=None, rebuild=True,
                       mock=False) -> dict:
    """字幕样式**唯一写入口**（Studio 放映区「字幕样式」面板）。

    `style` 按白名单合并进章节 `subtitle` 块（`sub_cfg` 的项目覆盖层——profile
    画风样式打底、这里逐键覆盖）：键值为 None → 删该键（回落画风缺省）；
    `style=None` → 整组样式键全部回落画风缺省（保留 lang/mode 等行为键）。
    字幕是合成期烧录进画面的，改完必须重合成才可见——`rebuild=True` 走
    `rebuild_final` 一条链（assemble --draft 重烧字幕 → 有水印再刷 output_wm），
    绝不在这里另起水印任务（与 `set_watermark` 的防竞态口径一致）。
    """
    with _exclusive(ws_root, pid, cid, "subtitle-style") as project:
        sub = dict(project.data.get("subtitle") or {})
        if style is None:
            for k in _SUBTITLE_STYLE_KEYS:
                sub.pop(k, None)
        else:
            unknown = [k for k in style if k not in _SUBTITLE_STYLE_KEYS]
            if unknown:
                raise KinemaError(f"不支持的字幕样式键: {', '.join(unknown)}"
                                  f"（可用: {', '.join(_SUBTITLE_STYLE_KEYS)}）")
            for k, v in style.items():
                if v is None or v == "":
                    sub.pop(k, None)
                else:
                    sub[k] = v
        if sub:
            project.data["subtitle"] = sub
        else:
            project.data.pop("subtitle", None)
        project.save()
    out = {"subtitle": {k: sub.get(k) for k in _SUBTITLE_STYLE_KEYS if k in sub}}
    if rebuild:
        out.update(rebuild_final(ws_root, pid, cid, mock=mock))
    return out


def rebuild_final(ws_root, pid, cid, *, mock=False) -> dict:
    """一键「重新构建」：按当前设置从**已生成的图/配音**重烧成片（不重跑生图/配音/图生视频）——
      1) `assemble --draft`：重新合成，**重新烧录字幕**（当前换行/边距逻辑）+ 应用当前特效
         + 重混音频 → 新 output（compose 最终一帧总是重渲，字幕必然按最新代码重烧）；
      2) 若配了漂移/固定水印：`watermark --from-project --force` → 用新 output 刷新 output_wm。
    走 `--draft` 绕过合成审阅闸（成片已存在，只是换设置重出）。字幕/特效/水印改了但成片是旧的
    时点它一键刷新——这是水印/特效等「后置设置」变更后让成片跟上的唯一入口。"""
    project = _load(ws_root, pid, cid)
    if not (project.data.get("output") or {}):
        raise KinemaError("还没有成片可重新构建——先完整合成一次再来重烧。")
    steps = [["assemble", "--chapter", f"{pid}/{cid}", "--draft"]]
    if mock:
        steps[0].append("--mock")
    floating, fixed, bottom = _wm_active(project)
    if floating or fixed or bottom:            # 有水印 → 用新成片刷新水印版
        steps.append(["watermark", "--chapter", f"{pid}/{cid}", "--from-project", "--force"])
    elif project.data.get("output_wm"):
        # 字段全空而水印版还在盘上：同一次提交「清空全部水印 + 改字幕样式」时，
        # 水印走 `burn=False` 只写盘（删除水印版的收口不在那条路上）——这里不补
        # 清理，页面会继续默认播那条旧字幕旧水印的片
        _remove_wm_version(project)
    from . import jobs
    jid = jobs.spawn_seq(steps, label=f"重新构建 {pid}/{cid}", ws_root=ws_root,
                         meta={"project": pid, "chapter": cid, "kind": "rebuild"})
    return {"job": jid, "steps": len(steps),
            "rewatermark": bool(floating or fixed or bottom)}


def verify_final(ws_root, pid, cid, *, samples=None) -> dict:
    """一键「自审」：后台跑 `verify --chapter pid/cid`——黑屏/该响却哑/削波/响度/
    时长/字幕/人声等机器体检，结论写章节 json 顶层 `verify`（scanner 原样透传）。

    零 API 成本（纯本地 ffmpeg 探测），只读产物。走子进程而非进程内直调：
    与 CLI 完全同一条路径，探测再慢也不占 HTTP 线程。"""
    project = _load(ws_root, pid, cid)
    if not {a: p for a, p in (project.data.get("output") or {}).items()
            if isinstance(p, str) and p}:
        raise KinemaError("还没有成片可自审——先完整合成一次再来体检。")
    args = ["verify", "--chapter", f"{pid}/{cid}"]
    if samples:
        args += ["--samples", str(int(samples))]
    from . import jobs
    jid = jobs.spawn_cli(args, label=f"成片自审 {pid}/{cid}", ws_root=ws_root,
                         meta={"project": pid, "chapter": cid, "kind": "verify"})
    return {"job": jid}


# ---- 风格垫图（项目级参考图，注入每张设定图/分镜图生成的 ref_images）----
def add_moodboard(ws_root, pid, *, path) -> dict:
    """登记一张已落盘的风格垫图（绝对路径）→ 系列 moodboard + 同步各章 style。"""
    from ..workspace import Workspace
    series = Workspace.open(str(ws_root), create=False).get_project(pid)
    series.add_moodboard(str(path))
    return {"moodboard": series.moodboard}


def remove_moodboard(ws_root, pid, *, path) -> dict:
    """移除一张风格垫图：清系列/各章登记，并删工作区内的文件。"""
    from ..workspace import Workspace
    series = Workspace.open(str(ws_root), create=False).get_project(pid)
    ok = series.remove_moodboard(str(path))
    try:                                   # 删文件（仅限工作区内，防越界删）
        fp = Path(path).resolve()
        if ok and fp.is_file() and str(fp).startswith(str(Path(ws_root).resolve())):
            fp.unlink()
    except Exception:  # noqa: BLE001  文件缺失/权限不阻断登记清理
        pass
    return {"removed": ok, "moodboard": series.moodboard}


def toggle_moodboard(ws_root, pid, *, path, on) -> dict:
    """切换某垫图的「默认启用」态（不删文件）：on=True 默认套用全局生成，False 留库靠提示词。"""
    from ..workspace import Workspace
    series = Workspace.open(str(ws_root), create=False).get_project(pid)
    changed = series.set_moodboard_on(str(path), bool(on))
    return {"changed": changed, "moodboard": series.moodboard}


def set_shot_refs(ws_root, pid, cid, *, shot, refs) -> dict:
    """镜级参考库覆盖：写 shots[].refs（网页勾选/取消垫图的落点）。

    refs 为库图绝对路径列表：
      · None      → 删除覆盖，回落默认生效集（参考库 on=True 全套）；
      · []        → 本镜刻意不用任何垫图（只靠提示词）；
      · [路径,…]  → 本镜精确只用这几张。
    不触发生成：已出画布的镜置 image retake，随后的 gen-image 按重做重生；
    已通过锁定的镜只回报 `locked`，锁由人解。"""
    with _exclusive(Path(ws_root), pid, cid, "shot-refs") as proj:
        s = _find_shot(proj, shot)
        if refs is None:
            s.pop("refs", None)
        else:
            s["refs"] = [str(x) for x in refs]
        # 垫图集合是生图输入：已出的画布按旧集合生成，未锁定即进重做队列，否则
        # 随后的 gen-image 看产物在盘照样跳过
        image_state = None
        if (s.get("image") or s.get("images")) and not review.is_locked(s, "image"):
            if not review.needs_retake(s, "image"):
                review.set_state(s, "image", "retake")
            image_state = "retake"
        elif review.is_locked(s, "image"):
            image_state = "locked"
        proj.save()
    return {"shot": shot, "refs": s.get("refs"), "image": image_state}


def _asset_ref_holder(series, kind, name):
    """定位设定图垫图字段的宿主 dict + 键：角色/道具=实体 dict 的 'refs'；
    具名取景地=`scenes[]` 条目的 'refs'；全局场景图=系列文档 'scene_refs'。
    scene 的 name 分派与 `refine._asset_refs`/gen-refs 读侧同判据——这里丢了
    name 的话，取景地灯箱勾的垫图会写进全局字段，重生时读 `scenes[].refs`
    仍为空，勾选形同虚设。"""
    if kind == "character":
        c = next((x for x in series.characters if x.get("name") == name), None)
        if not c:
            raise KinemaError(f"找不到角色 {name}")
        return c, "refs"
    if kind == "prop":
        p0 = next((x for x in series.props if x.get("name") == name), None)
        if not p0:
            raise KinemaError(f"找不到道具 {name}")
        return p0, "refs"
    if kind == "scene":
        if name:
            sc = next((x for x in series.scenes if x.get("name") == name), None)
            if not sc:
                raise KinemaError(f"找不到取景地 {name}")
            return sc, "refs"
        return series.data, "scene_refs"
    if kind == "topview":
        # 俯视布局图不吃风格垫图：它是制图，参考只有该场景的基准图（生成侧同判据，
        # 见 `cli.cmd_gen_refs` 第二波与 `refine.refine_asset`）。前端据此不给它
        # 「⛭ 垫图参考」入口；这条报错是绕过前端直调时的兜底，不是可配置项。
        raise KinemaError("场景俯视图不使用风格垫图——它以该场景的基准图为参考；"
                          "要换参考请用「⇪ 素材直供」上传现成平面图")
    raise KinemaError(f"未知资产类型: {kind}（可选: character / scene / prop）")


def set_asset_refs(ws_root, pid, *, kind, name=None, refs=None) -> dict:
    """设定图（角色/场景/道具）逐张垫图覆盖：写实体 refs / 系列 scene_refs（网页设定图灯箱勾选的落点）。

    refs 语义同 set_shot_refs：None=删覆盖回落默认集 · []=本图不用垫图 · [路径…]=精确只用这几张。
    仅改数据契约，不触发生成——由调用方随后跑 `project refs --only <kind>[:名] --force` 重生。"""
    from ..workspace import Workspace
    series = Workspace.open(str(ws_root), create=False).get_project(pid)
    with series.commit():                      # 勾垫图与设定图重生子进程写同一份文档
        holder, key = _asset_ref_holder(series, kind, name)   # 进锁后重新定位
        if refs is None:
            holder.pop(key, None)
        else:
            holder[key] = [str(x) for x in refs]
        current = holder.get(key)
    return {"kind": kind, "name": name, "refs": current}


def regen_asset_refs(ws_root, pid, *, kind, name=None, mock=False) -> dict:
    """设定图「按新垫图重新生成」：后台跑 `project refs --only <kind>[:名] --force`（全新出图·
    非 refine 局部改造）→ 用刚存的逐张垫图重出该张设定图，血缘随之传播下游分镜。
    与 regen_asset(refine 走批注)分工：这条是垫图变更后的纯重生，无需先提意见。"""
    from ..workspace import Workspace
    from . import jobs
    _asset_ref_holder(  # 先校验 kind/name 合法（找不到即抛，不白起任务）
        Workspace.open(str(ws_root), create=False).get_project(pid), kind, name)
    asset = f"{kind}:{name}" if name else kind
    args = ["project", "refs", pid, "--only", asset, "--force"]
    if mock:
        args.append("--mock")
    jid = jobs.spawn_cli(args, label=f"{pid} 设定图 {asset} 按新垫图重生", ws_root=ws_root,
                         meta={"project": pid, "kind": "regen_asset", "asset": asset})
    return {"job": jid}


def supply_asset_sheet(ws_root, pid, *, kind, name=None, path, skip_check=False) -> dict:
    """素材直供设定图（纯替换，不调模型）——规则与版本栈全在 `refine.supply_asset_sheet`。"""
    from ..refine import supply_asset_sheet as _supply
    from ..workspace import Workspace
    ws = Workspace.open(str(ws_root), create=False)
    return _supply(ws, pid, kind=kind, name=name, src=path,
                   skip_check=bool(skip_check))


def rollback_asset_version(ws_root, pid, *, kind, name=None, to) -> dict:
    """设定图版本回滚：把某历史版拷回标准路径（当前版先归档），血缘传播下游分镜标过期。
    与分镜 rollback_version 对称——设定图落系列文档、无章节/阶段。"""
    from ..workspace import Workspace
    from .. import refine
    series = Workspace.open(str(ws_root), create=False).get_project(pid)
    retaken, flagged = refine.rollback_asset_sheet(series, kind, name, int(to))
    return {"kind": kind, "name": name, "now_contains": f"v{to}",
            "stale_retaken": retaken, "stale_flagged": flagged}


# ---- 模型配置中心（与 CLI `config` 同一条写路径）----
# Studio 启动时若带了 --config，后续每次新读都必须读同一份——「新读一份」与
# 「读哪一份」是两件事，只该改前者；不记住它，配置页与其余接口会各读一份配置。
_CONFIG_PATH: str | None = None


def bind_config_path(path: str | None) -> None:
    """serve() 启动时登记 --config 指定的配置文件路径。

    同时经 `KINEMA_MODELS` 下发给后台任务与预览编译的子进程——它们各自重新
    发现配置，只有环境变量这一条进程外通道；不下发就是页面按一份配置标注、
    实发按另一份。"""
    import os
    global _CONFIG_PATH
    _CONFIG_PATH = path
    if path:
        os.environ["KINEMA_MODELS"] = str(path)


def _fresh_store():
    """取进程内共享的 ConfigStore（配置文件有变更则按 mtime 原地重载）。

    语义别名的存在理由：写路径必须永远拿到**当前**配置——`serve()` 若持有启动
    快照，网页改配置就成了「保存成功、页面显示旧值，直到 studio --restart」。
    失效判据收在 `ConfigStore.shared` 一处，调用点不必各自记「绕开长驻 store」
    这类纪律（逐调用点各管一份，忘一处就复发一次）。
    """
    from ..models import ConfigStore
    return ConfigStore.shared(_CONFIG_PATH)


def config_view(ws_root) -> dict:
    """配置中心的整份只读视图。先从数据库回流一次（换机继承），再按生效值组装。"""
    from .. import config_overlay as ovl
    ovl.pull(ws_root)
    return ovl.config_view(_fresh_store(), ws_root)


def set_model_config(ws_root, *, providers=None, defaults=None) -> dict:
    """写连接段与激活项（三态：字段缺省=不动 · 空=清除该覆盖 · 非空=覆盖）。

    返回**生效后**的整份视图，让前端直接回显解析结果，而不是自己再算一遍合并——
    两份合并逻辑一旦分叉，就会出现「页面显示的值 ≠ 真正发出去的值」。
    """
    from .. import config_overlay as ovl
    if defaults:
        store = _fresh_store()
        defaults = dict(defaults)
        for cap, alias in list(defaults.items()):
            if alias in (None, ""):
                continue
            conn = store.provider_conn(alias)      # 未知别名在这里就抛，不等到发请求
            kind = conn.get("kind")
            if kind and kind != cap:
                raise KinemaError(
                    f"provider '{alias}' 是 {kind} 能力，不能激活给 {cap}")
            # 落盘用归一后的新名（旧名兼容位只在读时认，不该沉淀进覆盖层）
            defaults[cap] = conn.get("name", alias)
    ovl.save(providers=providers, defaults=defaults, ws_root=ws_root, now=_now())
    return config_view(ws_root)


def set_provider_secret(ws_root, *, env, value) -> dict:
    """写/清一个密钥。**只写不读**——返回体只带状态，没有任何出口回读明文。"""
    from .. import config_overlay as ovl
    r = ovl.write_secret(env, value)
    return {"key": r, "config": config_view(ws_root)}


def test_provider(ws_root, *, alias=None) -> dict:
    """连通性自检（零成本：只查解析层，一个生成请求都不发）。"""
    from .. import config_overlay as ovl
    store = _fresh_store()
    aliases = [alias] if alias else sorted(store.data.get("providers") or {})
    return {"results": [ovl.probe(store, a) for a in aliases]}
