from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from pathlib import Path
from threading import Thread
from typing import cast

from cloud_strategy_platform.api import (
    ApiApplication,
    build_http_server,
    encode_sse_event,
)
from cloud_strategy_platform.contracts import AccessScope, MarketDataRuntimeState
from cloud_strategy_platform.feature_store import RawSipEventStore, SharedFeatureStore
from cloud_strategy_platform.market_data import SipBar
from cloud_strategy_platform.market_health import build_historical_coverage
from cloud_strategy_platform.registry import StrategyRegistry

NOW = datetime(2026, 7, 22, 15, 45, tzinfo=UTC)


class EmptyHistoricalMarketData:
    def bars(
        self, *, symbols: tuple[str, ...], start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]:
        return ()

    def quotes(
        self, *, symbols: tuple[str, ...], start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]:
        return ()

    def news(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]:
        return ()


def _application(
    tmp_path: Path,
) -> tuple[ApiApplication, StrategyRegistry, RawSipEventStore, str]:
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    raw = RawSipEventStore(tmp_path / "raw.sqlite3")
    token = registry.issue_token(
        principal_id="ai-market", scope=AccessScope.MARKET_DATA_READ
    )
    application = ApiApplication(
        registry=registry,
        feature_store=SharedFeatureStore(tmp_path / "features.sqlite3"),
        raw_store=raw,
        market_data=EmptyHistoricalMarketData(),
        clock=lambda: NOW,
    )
    return application, registry, raw, token


def _record_healthy_runtime(registry: StrategyRegistry) -> None:
    registry.record_market_runtime(
        state=MarketDataRuntimeState.CONNECTED,
        symbols=("AAPL",),
        process_started_at_utc=NOW - timedelta(minutes=10),
        heartbeat_at_utc=NOW,
        connected_at_utc=NOW - timedelta(minutes=9),
        last_event_at_utc=NOW,
        last_error_code=None,
        last_error_at_utc=None,
        reconnect_count=0,
    )


def test_health_is_liveness_but_ready_requires_a_recent_sip_owner(
    tmp_path: Path,
) -> None:
    application, registry, _, _ = _application(tmp_path)
    now = NOW
    registry.set_market_subscription(
        principal_id="ai-market",
        symbols=("AAPL",),
        updated_at_utc=now - timedelta(minutes=1),
        expires_at_utc=now + timedelta(hours=1),
    )

    health = application.handle(method="GET", target="/health", headers={})
    not_ready = application.handle(method="GET", target="/ready", headers={})
    assert health.status == 200
    assert health.body == {"api_version": "v1", "status": "ready"}
    assert not_ready.status == 503
    assert not_ready.body["status"] == "not_ready"
    assert not_ready.body["components"]["sip_owner"] == "not_ready"

    _record_healthy_runtime(registry)
    ready = application.handle(method="GET", target="/ready", headers={})
    assert ready.status == 200
    assert ready.body["status"] == "ready"
    assert ready.body["components"]["sip_owner"] == "ready"


def test_market_status_reports_subscription_bounds_freshness_and_fallback(
    tmp_path: Path,
) -> None:
    application, registry, raw, token = _application(tmp_path)
    registry.set_market_subscription(
        principal_id="ai-market",
        symbols=("AAPL",),
        updated_at_utc=NOW - timedelta(minutes=1),
        expires_at_utc=NOW + timedelta(hours=1),
    )
    _record_healthy_runtime(registry)
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

    response = application.handle(
        method="GET",
        target="/v1/market-data/status?symbols=AAPL,SMCI",
        headers={"authorization": f"Bearer {token}"},
    )

    assert response.status == 200
    assert response.body["status"] == "degraded"
    assert response.body["fallback_recommended"] is True
    assert response.body["subscription"]["active_symbols"] == ["AAPL"]
    by_symbol = {item["symbol"]: item for item in response.body["symbols"]}
    assert by_symbol["AAPL"]["subscribed"] is True
    assert by_symbol["AAPL"]["status"] == "healthy"
    assert by_symbol["AAPL"]["first_event_at_utc"] == NOW.isoformat()
    assert by_symbol["AAPL"]["last_bar_at_utc"] == NOW.isoformat()
    assert by_symbol["SMCI"]["subscribed"] is False
    assert by_symbol["SMCI"]["status"] == "unavailable"
    assert "not_subscribed" in by_symbol["SMCI"]["reason_codes"]
    assert response.body["execution_health"]["status"] == "external_not_observed"


def test_empty_bars_are_diagnostic_instead_of_an_ambiguous_empty_list(
    tmp_path: Path,
) -> None:
    application, _, _, token = _application(tmp_path)
    response = application.handle(
        method="GET",
        target=(
            "/v1/market-data/bars?symbols=SMCI"
            "&start=2026-07-22T15%3A44%3A00Z"
            "&end=2026-07-22T15%3A46%3A00Z"
        ),
        headers={"authorization": f"Bearer {token}"},
    )

    assert response.status == 200
    assert response.body["bars"] == []
    coverage = response.body["coverage"]
    assert coverage["status"] == "empty"
    assert coverage["fallback_recommended"] is True
    assert coverage["symbols"][0]["symbol"] == "SMCI"
    assert coverage["symbols"][0]["row_count"] == 0
    assert "upstream_empty" in coverage["symbols"][0]["reason_codes"]
    assert "regular_session_missing" in coverage["symbols"][0]["reason_codes"]


def test_bar_coverage_identifies_missing_minutes_without_calling_them_complete() -> None:
    coverage = build_historical_coverage(
        kind="bars",
        symbols=("AAPL",),
        start_utc=datetime(2026, 7, 22, 15, 44, tzinfo=UTC),
        end_utc=datetime(2026, 7, 22, 15, 47, tzinfo=UTC),
        rows=(
            {"symbol": "AAPL", "ts_utc": "2026-07-22T15:44:00Z"},
            {"symbol": "AAPL", "ts_utc": "2026-07-22T15:46:00Z"},
        ),
        now_utc=NOW,
    )

    assert coverage["status"] == "gaps_detected"
    symbols = coverage["symbols"]
    assert isinstance(symbols, list)
    symbol = symbols[0]
    assert isinstance(symbol, dict)
    assert symbol["missing_minute_count"] == 1
    assert symbol["missing_intervals"] == [
        {
            "start_utc": "2026-07-22T15:45:00+00:00",
            "end_utc": "2026-07-22T15:46:00+00:00",
            "missing_minutes": 1,
        }
    ]
    assert coverage["calendar_basis"] == "observed regular-session continuity"


def test_sse_event_is_resumable_and_contains_only_the_normalized_event() -> None:
    event = SipBar(
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

    frame = encode_sse_event(42, event)

    assert frame.startswith(b"id: 42\nevent: market-data\n")
    payload_line = next(line for line in frame.decode().splitlines() if line.startswith("data: "))
    payload = json.loads(payload_line.removeprefix("data: "))
    assert payload == {"sequence": 42, "event": event.model_dump(mode="json")}
    assert "key" not in frame.decode().lower()


def test_http_sse_stream_pushes_a_persisted_event_with_resume_id(
    tmp_path: Path,
) -> None:
    application, _, raw, token = _application(tmp_path)
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
    server = build_http_server(application, host="127.0.0.1", port=0)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    host, port = cast(tuple[str, int], server.server_address)
    connection = HTTPConnection(host, port, timeout=2)
    try:
        connection.request(
            "GET",
            "/v1/market-data/stream?symbols=AAPL&after=0&heartbeat_seconds=5",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "text/event-stream",
            },
        )
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader("Content-Type") == (
            "text/event-stream; charset=utf-8"
        )
        lines = [response.readline().decode() for _ in range(6)]
        assert lines[0] == "retry: 1000\n"
        assert lines[2] == "id: 1\n"
        assert lines[3] == "event: market-data\n"
        assert json.loads(lines[4].removeprefix("data: "))["sequence"] == 1
    finally:
        connection.close()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
