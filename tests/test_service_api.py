from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from cloud_strategy_platform.alpaca_paper import (
    BrokerOrder,
    PaperAccount,
    PaperCloseRequest,
    PaperOrderRequest,
    PaperPosition,
)
from cloud_strategy_platform.api import ApiApplication
from cloud_strategy_platform.contracts import AccessScope
from cloud_strategy_platform.feature_store import RawSipEventStore, SharedFeatureStore
from cloud_strategy_platform.market_data import SipBar
from cloud_strategy_platform.registry import StrategyRegistry

NOW = datetime(2026, 7, 22, 15, 45, tzinfo=UTC)


class FakeHistoricalMarketData:
    def bars(
        self, *, symbols: tuple[str, ...], start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]:
        return (
            {
                "symbol": symbols[0],
                "ts_utc": start_utc.isoformat(),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.5,
                "volume": 10,
                "trade_count": 2,
                "vwap": 100.25,
                "source": "cloud.alpaca.sip",
                "feed": "sip",
                "adjustment": "split_adjusted",
            },
        )

    def quotes(
        self, *, symbols: tuple[str, ...], start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]:
        return ()

    def news(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]:
        return ()


class FakePaperBroker:
    def __init__(self) -> None:
        self.submitted = 0
        self.cancelled = 0

    def get_account(self) -> PaperAccount:
        return PaperAccount(
            status="ACTIVE",
            account_blocked=False,
            trading_blocked=False,
            equity="100000",
            last_equity="100000",
            buying_power="200000",
        )

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None:
        return None

    def list_positions(self) -> tuple[PaperPosition, ...]:
        return ()

    def list_open_orders(self) -> tuple[BrokerOrder, ...]:
        return ()

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder:
        self.submitted += 1
        return BrokerOrder(
            id="broker-1",
            client_order_id=request.client_order_id,
            symbol=request.symbol,
            qty=request.qty,
            filled_qty="0",
            status="new",
        )

    def submit_close_order_idempotent(self, request: PaperCloseRequest) -> BrokerOrder:
        raise AssertionError("not used")

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled += 1
        return True


def _application(tmp_path: Path) -> tuple[ApiApplication, StrategyRegistry, FakePaperBroker]:
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    raw = RawSipEventStore(tmp_path / "raw.sqlite3")
    raw.append(
        SipBar(
            symbol="AAPL",
            ts_utc=NOW,
            open=100,
            high=101,
            low=99,
            close=100.5,
            volume=10,
            trade_count=2,
            vwap=100.25,
            provenance="alpaca.sip.websocket@test",
        )
    )
    paper = FakePaperBroker()
    return (
        ApiApplication(
            registry=registry,
            feature_store=SharedFeatureStore(tmp_path / "features.sqlite3"),
            raw_store=raw,
            market_data=FakeHistoricalMarketData(),
            paper_broker=paper,
        ),
        registry,
        paper,
    )


def test_market_data_token_reads_events_and_history_but_cannot_trade(tmp_path: Path) -> None:
    application, registry, _ = _application(tmp_path)
    token = registry.issue_token(
        principal_id="ai-quant-market", scope=AccessScope.MARKET_DATA_READ
    )
    headers = {"authorization": f"Bearer {token}"}

    events = application.handle(
        method="GET",
        target="/v1/market-data/events?after=0&symbols=AAPL&limit=10",
        headers=headers,
    )
    assert events.status == 200
    assert len(events.body["events"]) == 1
    bars = application.handle(
        method="GET",
        target=(
            "/v1/market-data/bars?symbols=AAPL"
            "&start=2026-07-22T15%3A44%3A00Z&end=2026-07-22T15%3A46%3A00Z"
        ),
        headers=headers,
    )
    assert bars.status == 200
    assert len(bars.body["bars"]) == 1
    denied = application.handle(
        method="GET", target="/v1/paper/account", headers=headers
    )
    assert denied.status == 401


def test_market_data_write_token_leases_symbols_and_read_token_cannot(
    tmp_path: Path,
) -> None:
    application, registry, _ = _application(tmp_path)
    write_token = registry.issue_token(
        principal_id="ai-quant-market", scope=AccessScope.MARKET_DATA_WRITE
    )
    read_token = registry.issue_token(
        principal_id="read-only", scope=AccessScope.MARKET_DATA_READ
    )
    body: dict[str, object] = {
        "symbols": ["msft", "AAPL", "MSFT"],
        "replay_from_utc": (NOW - timedelta(minutes=1)).isoformat(),
        "expires_at_utc": (NOW + timedelta(hours=1)).isoformat(),
    }

    denied = application.handle(
        method="POST",
        target="/v1/market-data/subscriptions",
        headers={"authorization": f"Bearer {read_token}"},
        body=body,
    )
    assert denied.status == 401

    accepted = application.handle(
        method="POST",
        target="/v1/market-data/subscriptions",
        headers={"authorization": f"Bearer {write_token}"},
        body=body,
    )
    assert accepted.status == 200
    assert accepted.body["symbols"] == ["AAPL", "MSFT"]
    assert accepted.body["start_after_sequence"] == 0
    assert registry.active_market_symbols(at_utc=NOW) == ("AAPL", "MSFT")


def test_market_data_subscription_expiry_fails_closed(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    registry.set_market_subscription(
        principal_id="ai-quant-market",
        symbols=("AAPL",),
        expires_at_utc=NOW + timedelta(minutes=1),
        updated_at_utc=NOW,
    )
    assert registry.active_market_symbols(at_utc=NOW) == ("AAPL",)
    assert registry.active_market_symbols(
        at_utc=NOW + timedelta(minutes=1)
    ) == ()


def test_paper_write_token_reads_account_and_submits_but_signal_token_cannot(
    tmp_path: Path,
) -> None:
    application, registry, paper = _application(tmp_path)
    paper_token = registry.issue_token(
        principal_id="ai-quant-execution", scope=AccessScope.PAPER_WRITE
    )
    signal_token = registry.issue_token(
        principal_id="alice", scope=AccessScope.SIGNALS_READ, strategy_id="default"
    )
    account = application.handle(
        method="GET",
        target="/v1/paper/account",
        headers={"authorization": f"Bearer {paper_token}"},
    )
    assert account.status == 200
    order = application.handle(
        method="POST",
        target="/v1/paper/orders",
        headers={"authorization": f"Bearer {paper_token}"},
        body={
            "kind": "entry",
            "request": {
                "client_order_id": "plan-1",
                "symbol": "AAPL",
                "qty": 1,
                "take_profit_price": "102",
                "stop_loss_price": "99",
            },
        },
    )
    assert order.status == 200
    assert paper.submitted == 1
    denied = application.handle(
        method="POST",
        target="/v1/paper/orders",
        headers={"authorization": f"Bearer {signal_token}"},
        body={"kind": "entry", "request": {}},
    )
    assert denied.status == 401
