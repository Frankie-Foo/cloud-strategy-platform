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

from cloud_strategy_platform.contracts import MarketDataRuntimeState
from cloud_strategy_platform.feature_store import RawSipEventStore, SharedFeatureStore
from cloud_strategy_platform.market_data import AlpacaSipStream, SipEvent, SipQuote
from cloud_strategy_platform.platform import CloudStrategyPlatform
from cloud_strategy_platform.registry import StrategyRegistry
from cloud_strategy_platform.runtime import ProcessLock

ROOT = Path(__file__).resolve().parents[1]


class SipStream(Protocol):
    connected_at_utc: datetime | None

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
    process_started_at_utc: datetime,
    reconnect_count: int,
) -> tuple[datetime | None, datetime | None]:
    loop = asyncio.get_running_loop()
    deadline = None if seconds is None else loop.time() + seconds
    # Publish the authenticated state promptly even when a quiet symbol emits no event.
    # Later heartbeats retain the caller-configured interval.
    next_refresh = loop.time() + min(refresh_seconds, 1.0)
    pending: asyncio.Task[SipEvent] | None = None
    connected_at: datetime | None = None
    last_event_at: datetime | None = None

    async def read_next(events: AsyncGenerator[SipEvent, None]) -> SipEvent:
        return await events.__anext__()

    async with aclosing(stream.events()) as events:
        try:
            while True:
                now = loop.time()
                connected_at = stream.connected_at_utc
                if deadline is not None and now >= deadline:
                    return connected_at, last_event_at
                if now >= next_refresh:
                    heartbeat_at = datetime.now(UTC)
                    current = registry.active_market_symbols(at_utc=heartbeat_at)
                    registry.record_market_runtime(
                        state=(
                            MarketDataRuntimeState.CONNECTED
                            if connected_at is not None
                            else MarketDataRuntimeState.CONNECTING
                        ),
                        symbols=symbols,
                        process_started_at_utc=process_started_at_utc,
                        heartbeat_at_utc=heartbeat_at,
                        connected_at_utc=connected_at,
                        last_event_at_utc=last_event_at,
                        last_error_code=None,
                        last_error_at_utc=None,
                        reconnect_count=reconnect_count,
                    )
                    if current != symbols:
                        return connected_at, last_event_at
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
                    return connected_at, last_event_at
                pending = None
                if _should_ingest(event, sampled_seconds=sampled_seconds):
                    platform.process_sip_event(event)
                    last_event_at = event.ts_utc
                    if connected_at is None:
                        connected_at = datetime.now(UTC)
                        registry.record_market_runtime(
                            state=MarketDataRuntimeState.CONNECTED,
                            symbols=symbols,
                            process_started_at_utc=process_started_at_utc,
                            heartbeat_at_utc=connected_at,
                            connected_at_utc=connected_at,
                            last_event_at_utc=last_event_at,
                            last_error_code=None,
                            last_error_at_utc=None,
                            reconnect_count=reconnect_count,
                        )
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
    process_started_at = datetime.now(UTC)
    reconnect_count = 0
    sampled_seconds: dict[str, datetime] = {}
    registry.record_market_runtime(
        state=MarketDataRuntimeState.STARTING,
        symbols=(),
        process_started_at_utc=process_started_at,
        heartbeat_at_utc=process_started_at,
        connected_at_utc=None,
        last_event_at_utc=None,
        last_error_code=None,
        last_error_at_utc=None,
        reconnect_count=reconnect_count,
    )
    while True:
        overall_remaining = _remaining(started, max_seconds=args.max_seconds)
        if overall_remaining is not None and overall_remaining <= 0:
            stopped_at = datetime.now(UTC)
            registry.record_market_runtime(
                state=MarketDataRuntimeState.STOPPED,
                symbols=(),
                process_started_at_utc=process_started_at,
                heartbeat_at_utc=stopped_at,
                connected_at_utc=None,
                last_event_at_utc=None,
                last_error_code=None,
                last_error_at_utc=None,
                reconnect_count=reconnect_count,
            )
            return
        heartbeat_at = datetime.now(UTC)
        symbols = registry.active_market_symbols(at_utc=heartbeat_at)
        if not symbols:
            registry.record_market_runtime(
                state=MarketDataRuntimeState.WAITING_FOR_SUBSCRIPTION,
                symbols=(),
                process_started_at_utc=process_started_at,
                heartbeat_at_utc=heartbeat_at,
                connected_at_utc=None,
                last_event_at_utc=None,
                last_error_code=None,
                last_error_at_utc=None,
                reconnect_count=reconnect_count,
            )
            await asyncio.sleep(
                args.idle_seconds
                if overall_remaining is None
                else min(args.idle_seconds, overall_remaining)
            )
            continue
        registry.record_market_runtime(
            state=MarketDataRuntimeState.CONNECTING,
            symbols=symbols,
            process_started_at_utc=process_started_at,
            heartbeat_at_utc=heartbeat_at,
            connected_at_utc=None,
            last_event_at_utc=None,
            last_error_code=None,
            last_error_at_utc=None,
            reconnect_count=reconnect_count,
        )
        connected_at: datetime | None = None
        last_event_at: datetime | None = None
        try:
            stream = stream_factory(
                api_key=os.environ["ALPACA_API_KEY_ID"],
                api_secret=os.environ["ALPACA_API_SECRET_KEY"],
                symbols=symbols,
            )
            connected_at, last_event_at = await _consume_window(
                stream=stream,
                platform=platform,
                registry=registry,
                symbols=symbols,
                sampled_seconds=sampled_seconds,
                refresh_seconds=args.refresh_seconds,
                seconds=overall_remaining,
                process_started_at_utc=process_started_at,
                reconnect_count=reconnect_count,
            )
            reconnect_count += 1
            reconnected_at = datetime.now(UTC)
            registry.record_market_runtime(
                state=MarketDataRuntimeState.RECONNECTING,
                symbols=symbols,
                process_started_at_utc=process_started_at,
                heartbeat_at_utc=reconnected_at,
                connected_at_utc=connected_at,
                last_event_at_utc=last_event_at,
                last_error_code=None,
                last_error_at_utc=None,
                reconnect_count=reconnect_count,
            )
        except Exception:
            reconnect_count += 1
            failed_at = datetime.now(UTC)
            registry.record_market_runtime(
                state=MarketDataRuntimeState.ERROR,
                symbols=symbols,
                process_started_at_utc=process_started_at,
                heartbeat_at_utc=failed_at,
                connected_at_utc=connected_at,
                last_event_at_utc=last_event_at,
                last_error_code="sip_stream_error",
                last_error_at_utc=failed_at,
                reconnect_count=reconnect_count,
            )
            await asyncio.sleep(
                1.0 if overall_remaining is None else min(1.0, overall_remaining)
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
