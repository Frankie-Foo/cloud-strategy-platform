from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import aclosing
from pathlib import Path

from cloud_strategy_platform.feature_store import RawSipEventStore, SharedFeatureStore
from cloud_strategy_platform.market_data import AlpacaSipStream
from cloud_strategy_platform.platform import CloudStrategyPlatform
from cloud_strategy_platform.registry import StrategyRegistry
from cloud_strategy_platform.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]


async def _run(args: argparse.Namespace) -> None:
    registry = StrategyRegistry(args.registry_db)
    symbols = tuple(
        sorted({symbol for strategy in registry.active_strategies() for symbol in strategy.symbols})
    )
    if not symbols:
        return
    platform = CloudStrategyPlatform(
        registry=registry,
        raw_store=RawSipEventStore(args.raw_db),
        feature_store=SharedFeatureStore(args.feature_db),
    )
    stream = AlpacaSipStream(
        api_key=os.environ["ALPACA_API_KEY_ID"],
        api_secret=os.environ["ALPACA_API_SECRET_KEY"],
        symbols=symbols,
    )
    started = asyncio.get_running_loop().time()
    async with aclosing(stream.events()) as events:
        while (
            args.max_seconds <= 0
            or asyncio.get_running_loop().time() - started < args.max_seconds
        ):
            remaining = (
                None
                if args.max_seconds <= 0
                else args.max_seconds - (asyncio.get_running_loop().time() - started)
            )
            try:
                event = (
                    await anext(events)
                    if remaining is None
                    else await asyncio.wait_for(anext(events), timeout=max(0.01, remaining))
                )
            except TimeoutError:
                break
            platform.process_sip_event(event)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry-db", type=Path, default=ROOT / "runs/platform.sqlite3")
    parser.add_argument("--raw-db", type=Path, default=ROOT / "runs/raw-sip.sqlite3")
    parser.add_argument("--feature-db", type=Path, default=ROOT / "runs/features.sqlite3")
    parser.add_argument("--lock-file", type=Path, default=ROOT / "runs/alpaca-sip.lock")
    parser.add_argument("--max-seconds", type=float, default=0.0)
    args = parser.parse_args()
    with ProcessLock(args.lock_file):
        asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
