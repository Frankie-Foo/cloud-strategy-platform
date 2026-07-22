from __future__ import annotations

import argparse
import os
from pathlib import Path

from dotenv import load_dotenv

from cloud_strategy_platform.alpaca_market_data import AlpacaHistoricalMarketData
from cloud_strategy_platform.alpaca_paper import AlpacaPaperBroker
from cloud_strategy_platform.api import ApiApplication, build_http_server
from cloud_strategy_platform.feature_store import RawSipEventStore, SharedFeatureStore
from cloud_strategy_platform.registry import StrategyRegistry

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--registry-db", type=Path, default=ROOT / "runs/platform.sqlite3")
    parser.add_argument("--feature-db", type=Path, default=ROOT / "runs/features.sqlite3")
    parser.add_argument("--raw-db", type=Path, default=ROOT / "runs/raw-sip.sqlite3")
    args = parser.parse_args()
    api_key = os.environ["ALPACA_API_KEY_ID"]
    api_secret = os.environ["ALPACA_API_SECRET_KEY"]
    writes_enabled = os.getenv("PAPER_BROKER_WRITE_ENABLED", "false").lower() == "true"
    application = ApiApplication(
        registry=StrategyRegistry(args.registry_db),
        feature_store=SharedFeatureStore(args.feature_db),
        raw_store=RawSipEventStore(args.raw_db),
        market_data=AlpacaHistoricalMarketData(
            api_key=api_key,
            api_secret=api_secret,
        ),
        paper_broker=AlpacaPaperBroker(
            api_key=api_key,
            api_secret=api_secret,
            writes_enabled=writes_enabled,
        ),
    )
    server = build_http_server(application, host=args.host, port=args.port)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
