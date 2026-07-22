from __future__ import annotations

import argparse
import json
from pathlib import Path

from cloud_strategy_platform.contracts import StrategyDefinition
from cloud_strategy_platform.registry import StrategyRegistry

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("definition", type=Path)
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--registry-db", type=Path, default=ROOT / "runs/platform.sqlite3")
    args = parser.parse_args()
    definition = StrategyDefinition.model_validate_json(
        args.definition.read_text(encoding="utf-8")
    )
    StrategyRegistry(args.registry_db).register(definition, activate=args.activate)
    print(json.dumps({"strategy_id": definition.strategy_id, "version": definition.version}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
