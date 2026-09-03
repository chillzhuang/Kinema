#!/usr/bin/env python3
"""重建 `.agents/skills` 发现别名（跨平台·幂等·一次性）。

`.agents/skills` 是指向 `.claude/skills` 的链接，供 Codex / Gemini CLI /
Amp / OpenCode 一族按 agentskills.io 开放标准自动发现 skill。POSIX 检出
天然就是 symlink，本脚本是空跑；**Windows** 上 git 未开 `core.symlinks`
时，链接会落地成一个写着目标路径的**文本残根**——没有任何破坏，只是
那些工具在这台机器上发现不了 skill。在仓库根跑一次：

    python tools/agents_alias.py

即把残根替换成 **NTFS junction**（目录联接：不需要管理员权限、不需要
开发者模式），并对 git 标记 `skip-worktree` 保持工作区干净。之后若想换
真 symlink：开发者模式 + `git config core.symlinks true` 后重检出即可。

唯一拒绝处理的情形是 `.agents/skills` 已是**实体目录**——那是第二份
真源，本脚本绝不代删用户数据，请人工确认后移除再重跑。
"""

import os
import subprocess
import sys
from pathlib import Path


def main(root=None) -> int:
    root = Path(root).resolve() if root else Path(__file__).resolve().parent.parent
    target = root / ".claude" / "skills"
    alias = root / ".agents" / "skills"
    if not target.is_dir():
        print("✗ 找不到 .claude/skills——请在仓库检出内运行", file=sys.stderr)
        return 1
    if (alias.is_symlink() or alias.exists()) \
            and alias.resolve() == target.resolve():
        print("✓ .agents/skills 已指向 .claude/skills，无需处理")
        return 0
    if alias.is_symlink() or alias.is_file():
        alias.unlink()  # 指错方向的链接 / Windows 文本残根
    elif alias.is_dir():
        print("✗ .agents/skills 是实体目录（第二份真源）——实体只准在 "
              ".claude/skills/，请人工确认内容后移除它再重跑", file=sys.stderr)
        return 1
    alias.parent.mkdir(exist_ok=True)
    if os.name == "posix":
        alias.symlink_to(Path("..") / ".claude" / "skills")
        print("✓ 已重建 symlink .agents/skills → ../.claude/skills")
        return 0
    try:
        import _winapi
        _winapi.CreateJunction(str(target), str(alias))
    except (ImportError, AttributeError, OSError):
        # 极旧 Python 兜底：junction 经 cmd 内建 mklink /J（同样免管理员）
        subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(target)],
                       check=True, capture_output=True)
    subprocess.run(["git", "update-index", "--skip-worktree", ".agents/skills"],
                   cwd=root, check=False, capture_output=True)
    print("✓ 已创建 NTFS junction（免管理员）并标记 git skip-worktree")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
