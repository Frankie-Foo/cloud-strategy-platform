from __future__ import annotations

import argparse
from pathlib import Path

from cloud_strategy_platform.api import ApiApplication, build_http_server
from cloud_strategy_platform.feature_store import SharedFeatureStore
from cloud_strategy_platform.registry import StrategyRegistry

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--registry-db", type=Path, default=ROOT / "runs/platform.sqlite3")
    parser.add_argument("--feature-db", type=Path, default=ROOT / "runs/features.sqlite3")
    args = parser.parse_args()
    application = ApiApplication(
        registry=StrategyRegistry(args.registry_db),
        feature_store=SharedFeatureStore(args.feature_db),
    )
    server = build_http_server(application, host=args.host, port=args.port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
