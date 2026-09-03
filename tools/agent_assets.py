#!/usr/bin/env python3
"""编译或检查 Kinema Agent/Skill 资产。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine"
if str(ENGINE) not in sys.path:
    sys.path.insert(0, str(ENGINE))

from kinema.agent_assets import AgentAssetError, check_assets, compile_assets  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Kinema Agent/Skill 确定性资产编译器")
    parser.add_argument("action", choices=("compile", "check"))
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)
    try:
        result = compile_assets(ROOT) if args.action == "compile" else check_assets(ROOT)
    except AgentAssetError as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        else:
            print(f"✗ {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif result["ok"]:
        print(f"✓ Agent assets {args.action}: {result['skills']} skills · "
              f"catalog {result['catalog_version']} · {result['manifest_digest']}")
    else:
        print("✗ Agent assets check 发现漂移:", file=sys.stderr)
        for error in result["errors"]:
            print(f"  - {error}", file=sys.stderr)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
