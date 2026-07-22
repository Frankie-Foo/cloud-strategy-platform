from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cloud_strategy_platform.feature_store import RawSipEventStore
from cloud_strategy_platform.market_data import SipBar, SipEvent, SipQuote
from cloud_strategy_platform.registry import StrategyRegistry
from scripts.run_sip_owner import _run, _should_ingest


class FakeSipStream:
    def __init__(self, *, symbols: tuple[str, ...]):
        self.symbols = symbols

    async def events(self) -> AsyncGenerator[SipEvent, None]:
        yield SipBar(
            symbol=self.symbols[0],
            ts_utc=datetime.now(UTC),
            open=100,
            high=101,
            low=99,
            close=100.5,
            volume=100,
            trade_count=10,
            vwap=100.25,
            provenance="test.sip-owner",
        )
        while True:
            await asyncio.sleep(1)


def test_owner_samples_one_quote_per_symbol_second_but_never_drops_bars() -> None:
    sampled_seconds: dict[str, datetime] = {}
    first = SipQuote(
        symbol="SMCI",
        ts_utc=datetime(2026, 7, 22, 14, 40, 1, 100, tzinfo=UTC),
        bid_price=30,
        bid_size=10,
        ask_price=30.01,
        ask_size=10,
        provenance="test",
    )
    duplicate_second = first.model_copy(
        update={"ts_utc": datetime(2026, 7, 22, 14, 40, 1, 900, tzinfo=UTC)}
    )
    next_second = first.model_copy(
        update={"ts_utc": datetime(2026, 7, 22, 14, 40, 2, tzinfo=UTC)}
    )
    bar = SipBar(
        symbol="SMCI",
        ts_utc=datetime(2026, 7, 22, 14, 40, tzinfo=UTC),
        open=30,
        high=30.1,
        low=29.9,
        close=30.05,
        volume=100,
        trade_count=10,
        vwap=30.02,
        provenance="test",
    )

    assert _should_ingest(first, sampled_seconds=sampled_seconds)
    assert not _should_ingest(duplicate_second, sampled_seconds=sampled_seconds)
    assert _should_ingest(next_second, sampled_seconds=sampled_seconds)
    assert _should_ingest(bar, sampled_seconds=sampled_seconds)


def test_owner_waits_for_a_subscription_then_ingests_events(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    registry_db = tmp_path / "registry.sqlite3"
    raw_db = tmp_path / "raw.sqlite3"
    args = argparse.Namespace(
        registry_db=registry_db,
        raw_db=raw_db,
        feature_db=tmp_path / "features.sqlite3",
        max_seconds=0.8,
        refresh_seconds=0.1,
        idle_seconds=0.005,
    )
    seen_symbols: list[tuple[str, ...]] = []

    def factory(**kwargs: object) -> FakeSipStream:
        symbols = kwargs["symbols"]
        assert isinstance(symbols, tuple)
        seen_symbols.append(symbols)
        return FakeSipStream(symbols=symbols)

    async def exercise() -> None:
        owner = asyncio.create_task(_run(args, stream_factory=factory))
        await asyncio.sleep(0.02)
        now = datetime.now(UTC)
        StrategyRegistry(registry_db).set_market_subscription(
            principal_id="ai-quant-market",
            symbols=("SMCI", "OKLO"),
            updated_at_utc=now,
            expires_at_utc=now + timedelta(minutes=5),
        )
        await owner

    monkeypatch.setenv("ALPACA_API_KEY_ID", "test-key")  # type: ignore[attr-defined]
    monkeypatch.setenv("ALPACA_API_SECRET_KEY", "test-secret")  # type: ignore[attr-defined]
    asyncio.run(exercise())

    assert seen_symbols
    assert len(seen_symbols) == 1
    assert all(symbols == ("OKLO", "SMCI") for symbols in seen_symbols)
    assert RawSipEventStore(raw_db).count() >= 1
