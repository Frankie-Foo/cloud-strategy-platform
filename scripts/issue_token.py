from __future__ import annotations

import argparse
import json
from pathlib import Path

from cloud_strategy_platform.contracts import AccessScope
from cloud_strategy_platform.registry import StrategyRegistry

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--principal", required=True)
    parser.add_argument("--scope", required=True, choices=tuple(AccessScope))
    parser.add_argument("--strategy-id")
    parser.add_argument("--registry-db", type=Path, default=ROOT / "runs/platform.sqlite3")
    args = parser.parse_args()
    token = StrategyRegistry(args.registry_db).issue_token(
        principal_id=args.principal,
        scope=AccessScope(args.scope),
        strategy_id=args.strategy_id,
    )
    print(json.dumps({"token": token, "shown_once": True}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
