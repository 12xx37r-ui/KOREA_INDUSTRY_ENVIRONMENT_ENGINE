from __future__ import annotations

import argparse
import json
from pathlib import Path

from .engine import run_engine


def main() -> int:
    parser = argparse.ArgumentParser(description="Korea Industry Environment Engine")
    parser.add_argument("--root", default=".")
    parser.add_argument("--fixture-dir", default="")
    parser.add_argument("--no-live-krx", action="store_true")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    fixture = Path(args.fixture_dir).resolve() if args.fixture_dir else None
    result = run_engine(root, fixture_dir=fixture, allow_live_krx=not args.no_live_krx)
    print(json.dumps({
        "status": result.get("status"),
        "engine_version": result.get("engine_version"),
        "industry_count": result.get("industry_count"),
        "generated_at_utc": result.get("generated_at_utc"),
        "call_efficiency": result.get("call_efficiency"),
        "prospective_validation": result.get("prospective_validation"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
