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

"""决策审计 `decisions[]`：指挥层在制作中做过的取舍，逐条留痕。

**为什么落章节文档而不是系列文档**：章节文档走 `Project.save` 的三方合并
（`_DOC_HUMAN_KEYS`），系列文档 `Series.save` 是零保护的整份盲覆盖。

**为什么必须走 CLI 而不是让指挥层裸改 JSON**——两条吞没路径，各治一条：
  ① 内存覆写：引擎长任务（gen-image/tts/gen-video）全程持有整份文档的旧内存副本
     并逐镜 `project.save()`。裸改磁盘后，下一次逐镜 save 会用不认识 decisions 的
     旧副本整份覆写——决策消失且零提示（与 review/comments 被吞的判例完全同型）。
     解法 = 登记进 `project._DOC_HUMAN_KEYS`，且合并规则用**按 id 取并集**
     （整键替换会在两侧同时 append 时丢掉一边，「按 id 追加去重」的语义随之失效）。
  ② 库覆写：mysql 模式下 `storage._row_newer` 的 2s 容差 + `load_chapter` 会用库
     版本**直接覆写本地文件**——裸改的 JSON 在 `Project.load` 之前就没了，
     `_DOC_HUMAN_KEYS` 根本救不到。只有走 `decision add`（load→append→save，
     save 顺带 upsert 入库）才安全。

**append-only 审计日志**：只增不改不删。合并层取并集意味着「删除」这个动作本身
无法经引擎传播（union 会把磁盘上的旧条目找回来）——这正是审计日志该有的性质，
所以不提供 `decision rm`。记错了就再记一条覆盖性的决策，历史保持可追。

**缺 id 的条目必须有确定性去重键**（`entry_key`）：schema 把 `id` 标成
`[engine-managed]`，而契约铁律是「engine-managed 字段你不要手写」——指挥层照章
裸改 JSON 写出的条目天然没有 id。去重键若只认 id，这类条目在 union 的两侧
（内存 + 磁盘）各留一份 → **每次 save 条数翻倍**，一趟 8 镜长任务就是 2^N 条、
上百 MB 的 project.json（Studio 的 JSON.parse 直接崩，与 NaN 落盘同型事故），
且 append-only + 取并集使其无法经引擎收回。故缺 id 时回退到**内容派生键**
（`sha256:<hex16>`，与血缘指纹同格式，一眼可辨非雪花），并在 union 时就地补进
条目 —— 文档第一次 save 即自愈成带 id 的合法条目。
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime

from .errors import ProjectError

CONFIDENCE = ("high", "medium", "low")   # 置信度用枚举而非数值：审计日志读的是人话，
DEFAULT_CONFIDENCE = "medium"            # 且枚举天然免疫 NaN/Infinity 落盘
KEY = "decisions"
MAX_TEXT = 500


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _clean(v, limit: int = MAX_TEXT) -> str:
    return str(v or "").strip()[:limit]


def entries(doc: dict) -> list[dict]:
    """章节文档里的决策条目（只读，永远返回 list）。"""
    v = (doc or {}).get(KEY)
    return [e for e in v if isinstance(e, dict)] if isinstance(v, list) else []


def add(doc: dict, *, choice: str, alternatives=(), why: str = "",
        confidence: str = DEFAULT_CONFIDENCE, at: str | None = None) -> dict:
    """追加一条决策，返回新条目（**就地改 doc，不落盘**——落盘由调用方 save）。

    `id` 用雪花（与 comments/batch_ops 同源），合并层按它取并集去重。
    """
    choice = _clean(choice)
    if not choice:
        raise ProjectError("决策内容（choice）不能为空")
    conf = str(confidence or DEFAULT_CONFIDENCE).strip().lower()
    if conf not in CONFIDENCE:
        raise ProjectError(f"confidence 只能是 {'/'.join(CONFIDENCE)}，收到: {confidence}")
    from .storage.snowflake import next_id
    entry = {
        "id": str(next_id()),
        "choice": choice,
        "alternatives": [_clean(a, 200) for a in (alternatives or []) if _clean(a, 200)][:10],
        "why": _clean(why),
        "confidence": conf,
        "at": at or _now(),
    }
    pool = doc.get(KEY)
    if not isinstance(pool, list):
        pool = []
        doc[KEY] = pool
    pool.append(entry)
    return entry


def derived_id(entry: dict) -> str:
    """缺 id 的条目的**内容派生 id**：`sha256:<hex16>`（与血缘指纹同格式）。

    只认内容、不含 id 本身，故同一条目在内存侧与磁盘侧派生出同一个键——这正是
    union 去重需要的确定性。代价是「内容逐字相同的两条」会被并成一条：审计日志里
    这两条本就不可区分，比起条数指数膨胀把文档写坏，这个取舍是划算的。
    """
    body = {k: v for k, v in entry.items() if k != "id"}
    try:
        blob = json.dumps(body, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):   # 手写 JSON 极难触发，但键必须永远算得出来
        blob = repr(sorted((str(k), repr(v)) for k, v in body.items()))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def entry_key(entry: dict) -> str:
    """条目的去重键：有 id 用 id，缺 id 回退内容派生键（见模块 docstring）。"""
    return str(entry.get("id") or "").strip() or derived_id(entry)


def union_by_id(mem, disk) -> list[dict]:
    """两份 decisions 按 `id` 取并集（合并层单一真源）。

    顺序 = 内存侧原序在前、磁盘侧新增的追加在后。**不是**整键替换：引擎内存副本
    与磁盘各自 append 过时，整键替换必然丢掉一边——那正是「按 id 追加去重」这句
    承诺的落点，写在写入层是治不了的（写入层根本看不见另一侧）。

    缺 id 的条目（指挥层裸改 JSON 写的）按 `entry_key` 的内容派生键去重，并**补上
    该 id 后再入列**：文档一次 save 即自愈，重复条目不再随每次 save 翻倍。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for e in list(mem or []) + list(disk or []):
        if not isinstance(e, dict):
            continue
        key = entry_key(e)
        if key in seen:
            continue
        seen.add(key)
        if str(e.get("id") or "").strip():
            out.append(e)
        else:                          # 就地补 id（放首位，与 add() 出产的条目同形）
            out.append({"id": key, **{k: v for k, v in e.items() if k != "id"}})
    return out


def report_lines(doc: dict) -> list[str]:
    """`decision list` 的可读渲染。"""
    items = entries(doc)
    if not items:
        return ["  （暂无决策记录）"]
    mark = {"high": "◆", "medium": "◇", "low": "·"}
    lines = []
    for i, e in enumerate(items, 1):
        conf = str(e.get("confidence") or DEFAULT_CONFIDENCE)
        lines.append(f"  {i:>2}. {mark.get(conf, '◇')} {e.get('choice', '')}"
                     f"   [{conf} · {e.get('at', '')}]")
        if e.get("alternatives"):
            lines.append(f"      备选: {' / '.join(str(a) for a in e['alternatives'])}")
        if e.get("why"):
            lines.append(f"      理由: {e['why']}")
    return lines
