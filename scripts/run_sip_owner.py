from __future__ import annotations

import argparse
import asyncio
import os
from collections.abc import AsyncGenerator, Callable
from contextlib import aclosing
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from dotenv import load_dotenv

from cloud_strategy_platform.feature_store import RawSipEventStore, SharedFeatureStore
from cloud_strategy_platform.market_data import AlpacaSipStream, SipEvent, SipQuote
from cloud_strategy_platform.platform import CloudStrategyPlatform
from cloud_strategy_platform.registry import StrategyRegistry
from cloud_strategy_platform.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]


class SipStream(Protocol):
    def events(self) -> AsyncGenerator[SipEvent, None]: ...


def _should_ingest(
    event: SipEvent,
    *,
    sampled_seconds: dict[str, datetime],
) -> bool:
    if not isinstance(event, SipQuote):
        return True
    second = event.ts_utc.replace(microsecond=0)
    previous = sampled_seconds.get(event.symbol)
    if previous is not None and second <= previous:
        return False
    sampled_seconds[event.symbol] = second
    return True


def _remaining(started: float, *, max_seconds: float) -> float | None:
    if max_seconds <= 0:
        return None
    return max(0.0, max_seconds - (asyncio.get_running_loop().time() - started))


async def _consume_window(
    *,
    stream: SipStream,
    platform: CloudStrategyPlatform,
    registry: StrategyRegistry,
    symbols: tuple[str, ...],
    sampled_seconds: dict[str, datetime],
    refresh_seconds: float,
    seconds: float | None,
) -> None:
    loop = asyncio.get_running_loop()
    deadline = None if seconds is None else loop.time() + seconds
    next_refresh = loop.time() + refresh_seconds
    pending: asyncio.Task[SipEvent] | None = None

    async def read_next(events: AsyncGenerator[SipEvent, None]) -> SipEvent:
        return await events.__anext__()

    async with aclosing(stream.events()) as events:
        try:
            while True:
                now = loop.time()
                if deadline is not None and now >= deadline:
                    return
                if now >= next_refresh:
                    current = registry.active_market_symbols(at_utc=datetime.now(UTC))
                    if current != symbols:
                        return
                    next_refresh = now + refresh_seconds
                timeout = max(0.001, next_refresh - now)
                if deadline is not None:
                    timeout = min(timeout, max(0.001, deadline - now))
                pending = pending or asyncio.create_task(read_next(events))
                done, _ = await asyncio.wait({pending}, timeout=timeout)
                if not done:
                    continue
                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                pending = None
                if _should_ingest(event, sampled_seconds=sampled_seconds):
                    platform.process_sip_event(event)
        finally:
            if pending is not None:
                pending.cancel()
                try:
                    await pending
                except (asyncio.CancelledError, StopAsyncIteration):
                    pass


async def _run(
    args: argparse.Namespace,
    *,
    stream_factory: Callable[..., SipStream] = AlpacaSipStream,
) -> None:
    registry = StrategyRegistry(args.registry_db)
    platform = CloudStrategyPlatform(
        registry=registry,
        raw_store=RawSipEventStore(args.raw_db),
        feature_store=SharedFeatureStore(args.feature_db),
    )
    started = asyncio.get_running_loop().time()
    sampled_seconds: dict[str, datetime] = {}
    while True:
        overall_remaining = _remaining(started, max_seconds=args.max_seconds)
        if overall_remaining is not None and overall_remaining <= 0:
            return
        symbols = registry.active_market_symbols(at_utc=datetime.now(UTC))
        if not symbols:
            await asyncio.sleep(
                args.idle_seconds
                if overall_remaining is None
                else min(args.idle_seconds, overall_remaining)
            )
            continue
        stream = stream_factory(
            api_key=os.environ["ALPACA_API_KEY_ID"],
            api_secret=os.environ["ALPACA_API_SECRET_KEY"],
            symbols=symbols,
        )
        await _consume_window(
            stream=stream,
            platform=platform,
            registry=registry,
            symbols=symbols,
            sampled_seconds=sampled_seconds,
            refresh_seconds=args.refresh_seconds,
            seconds=overall_remaining,
        )


def main() -> int:
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry-db", type=Path, default=ROOT / "runs/platform.sqlite3")
    parser.add_argument("--raw-db", type=Path, default=ROOT / "runs/raw-sip.sqlite3")
    parser.add_argument("--feature-db", type=Path, default=ROOT / "runs/features.sqlite3")
    parser.add_argument("--lock-file", type=Path, default=ROOT / "runs/alpaca-sip.lock")
    parser.add_argument("--max-seconds", type=float, default=0.0)
    parser.add_argument("--refresh-seconds", type=float, default=30.0)
    parser.add_argument("--idle-seconds", type=float, default=1.0)
    args = parser.parse_args()
    if args.refresh_seconds <= 0 or args.idle_seconds <= 0:
        parser.error("refresh and idle intervals must be positive")
    with ProcessLock(args.lock_file):
        asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
