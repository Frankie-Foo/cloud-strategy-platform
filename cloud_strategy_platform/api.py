"""Versioned HTTP boundary; raw SIP and order capabilities are intentionally absent."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Literal, Protocol
from urllib.parse import parse_qs, unquote, urlparse

from cloud_strategy_platform.alpaca_market_data import MarketDataError
from cloud_strategy_platform.alpaca_paper import (
    BrokerError,
    BrokerOrder,
    BrokerWritesDisabledError,
    PaperAccount,
    PaperCloseRequest,
    PaperOrderRequest,
    PaperPosition,
)
from cloud_strategy_platform.contracts import (
    API_VERSION,
    AccessScope,
    MarketDataRuntimeState,
    MarketDataSubscriptionRequest,
)
from cloud_strategy_platform.feature_store import RawSipEventStore, SharedFeatureStore
from cloud_strategy_platform.market_data import SipEvent
from cloud_strategy_platform.market_health import (
    build_historical_coverage,
    describe_market_session,
)
from cloud_strategy_platform.registry import AuthorizationError, StrategyRegistry


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: dict[str, Any]


@dataclass(frozen=True)
class MarketStreamRequest:
    symbols: tuple[str, ...]
    after_sequence: int
    heartbeat_seconds: float


def encode_sse_event(sequence: int, event: SipEvent) -> bytes:
    payload = json.dumps(
        {"sequence": sequence, "event": event.model_dump(mode="json")},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return f"id: {sequence}\nevent: market-data\ndata: {payload}\n\n".encode()


class HistoricalMarketData(Protocol):
    def bars(
        self, *, symbols: tuple[str, ...], start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]: ...

    def quotes(
        self, *, symbols: tuple[str, ...], start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]: ...

    def news(
        self, *, start_utc: datetime, end_utc: datetime
    ) -> tuple[dict[str, object], ...]: ...


class PaperBroker(Protocol):
    def get_account(self) -> PaperAccount: ...

    def get_order_by_client_id(self, client_order_id: str) -> BrokerOrder | None: ...

    def list_positions(self) -> tuple[PaperPosition, ...]: ...

    def list_open_orders(self) -> tuple[BrokerOrder, ...]: ...

    def submit_order_idempotent(self, request: PaperOrderRequest) -> BrokerOrder: ...

    def submit_close_order_idempotent(self, request: PaperCloseRequest) -> BrokerOrder: ...

    def cancel_order(self, order_id: str) -> bool: ...


class ApiApplication:
    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        feature_store: SharedFeatureStore,
        raw_store: RawSipEventStore | None = None,
        market_data: HistoricalMarketData | None = None,
        paper_broker: PaperBroker | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.registry = registry
        self.feature_store = feature_store
        self.raw_store = raw_store
        self.market_data = market_data
        self.paper_broker = paper_broker
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _bearer(headers: dict[str, str]) -> str:
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise AuthorizationError("missing bearer token")
        return authorization.removeprefix("Bearer ").strip()

    @staticmethod
    def _utc(value: str) -> datetime:
        parsed = datetime.fromisoformat(value.replace(" ", "+").replace("Z", "+00:00"))
        if parsed.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be timezone-aware UTC")
        return parsed

    @staticmethod
    def _symbols(value: str) -> tuple[str, ...]:
        symbols = tuple(sorted({item.strip().upper() for item in value.split(",") if item.strip()}))
        if not symbols:
            raise ValueError("symbols are required")
        return symbols

    def _runtime_is_ready(
        self, *, now_utc: datetime, active_symbols: tuple[str, ...]
    ) -> bool:
        runtime = self.registry.market_runtime()
        return bool(
            active_symbols
            and runtime is not None
            and runtime.state is MarketDataRuntimeState.CONNECTED
            and set(runtime.symbols) == set(active_symbols)
            and 0
            <= (now_utc - runtime.heartbeat_at_utc).total_seconds()
            <= 90
        )

    def _readiness(self) -> ApiResponse:
        now = self.clock()
        active_symbols = self.registry.active_market_symbols(at_utc=now)
        raw_ready = self.raw_store is not None
        owner_ready = raw_ready and self._runtime_is_ready(
            now_utc=now, active_symbols=active_symbols
        )
        ready = raw_ready and owner_ready
        return ApiResponse(
            HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
            {
                "api_version": API_VERSION,
                "status": "ready" if ready else "not_ready",
                "components": {
                    "api": "ready",
                    "raw_store": "ready" if raw_ready else "not_ready",
                    "sip_owner": "ready" if owner_ready else "not_ready",
                },
            },
        )

    @staticmethod
    def _parsed_utc(value: object) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.utcoffset() == timedelta(0) else None

    @staticmethod
    def _integer(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    def _market_status(
        self, *, query: dict[str, list[str]], now_utc: datetime
    ) -> ApiResponse:
        if self.raw_store is None:
            return ApiResponse(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "unavailable"})
        active_symbols = self.registry.active_market_symbols(at_utc=now_utc)
        requested = (
            self._symbols(query["symbols"][0])
            if query.get("symbols")
            else active_symbols
        )
        stats = self.raw_store.symbol_observability(requested)
        runtime = self.registry.market_runtime()
        runtime_ready = self._runtime_is_ready(
            now_utc=now_utc, active_symbols=active_symbols
        )
        session = describe_market_session(now_utc)
        symbol_results: list[dict[str, object]] = []
        for symbol in requested:
            observed = stats.get(symbol, {})
            last_event = self._parsed_utc(observed.get("last_event_at_utc"))
            age_seconds = (
                None
                if last_event is None
                else max(0.0, (now_utc - last_event).total_seconds())
            )
            reasons: list[str] = []
            subscribed = symbol in active_symbols
            if not subscribed:
                status = "unavailable"
                reasons.append("not_subscribed")
            elif not runtime_ready:
                status = "unavailable"
                reasons.append("sip_owner_not_ready")
            elif session["session"] == "closed":
                status = "market_closed"
                reasons.append("outside_extended_session")
            elif age_seconds is None:
                status = "unavailable"
                reasons.append("no_events_observed")
            elif age_seconds <= 90:
                status = "healthy"
            elif age_seconds <= 300:
                status = "delayed"
                reasons.append("event_delay_above_90_seconds")
            else:
                status = "stale"
                reasons.append("event_delay_above_300_seconds")
            symbol_results.append(
                {
                    "symbol": symbol,
                    "subscribed": subscribed,
                    "status": status,
                    "first_event_at_utc": observed.get("first_event_at_utc"),
                    "last_event_at_utc": observed.get("last_event_at_utc"),
                    "last_bar_at_utc": observed.get("last_bar_at_utc"),
                    "last_quote_at_utc": observed.get("last_quote_at_utc"),
                    "last_event_age_seconds": age_seconds,
                    "event_count": self._integer(observed.get("event_count")),
                    "bar_count": self._integer(observed.get("bar_count")),
                    "quote_count": self._integer(observed.get("quote_count")),
                    "last_sequence": observed.get("last_sequence"),
                    "reason_codes": reasons,
                }
            )
        statuses = {str(item["status"]) for item in symbol_results}
        if not symbol_results or statuses <= {"unavailable", "stale"}:
            overall = "unavailable"
        elif statuses == {"healthy"}:
            overall = "healthy"
        elif statuses == {"market_closed"}:
            overall = "market_closed"
        else:
            overall = "degraded"
        fallback = any(
            item["status"] in {"unavailable", "stale", "delayed"}
            for item in symbol_results
        )
        owner: dict[str, object]
        if runtime is None:
            owner = {
                "state": "unavailable",
                "heartbeat_at_utc": None,
                "heartbeat_age_seconds": None,
                "connected_at_utc": None,
                "last_event_at_utc": None,
                "last_error_code": None,
                "last_error_at_utc": None,
                "reconnect_count": 0,
                "symbols": [],
            }
        else:
            owner = runtime.model_dump(mode="json")
            owner["heartbeat_age_seconds"] = max(
                0.0, (now_utc - runtime.heartbeat_at_utc).total_seconds()
            )
        return ApiResponse(
            HTTPStatus.OK,
            {
                "api_version": API_VERSION,
                "observed_at_utc": now_utc.isoformat(),
                "status": overall,
                "fallback_recommended": fallback,
                "market_session": session,
                "owner": owner,
                "subscription": {
                    "active_symbols": list(active_symbols),
                    "requested_symbols": list(requested),
                    "active_symbol_count": len(active_symbols),
                },
                "symbols": symbol_results,
                "execution_health": {
                    "status": "external_not_observed",
                    "reason": (
                        "execution is owned by an external IBKR simulation link; "
                        "market health does not imply order availability or fills"
                    ),
                },
            },
        )

    def prepare_market_stream(
        self, *, target: str, headers: dict[str, str]
    ) -> MarketStreamRequest | ApiResponse:
        try:
            token = self._bearer(headers)
            self.registry.authorize(token, scope=AccessScope.MARKET_DATA_READ)
            if self.raw_store is None:
                return ApiResponse(
                    HTTPStatus.SERVICE_UNAVAILABLE, {"error": "unavailable"}
                )
            parsed = urlparse(target)
            query = parse_qs(parsed.query)
            symbols = self._symbols(query.get("symbols", [""])[0])
            if len(symbols) > 500:
                raise ValueError("too many stream symbols")
            cursor_value = query.get("after", [headers.get("last-event-id", "0")])[0]
            after = int(cursor_value)
            heartbeat = float(query.get("heartbeat_seconds", ["15"])[0])
            if after < 0 or not 5 <= heartbeat <= 60:
                raise ValueError("stream parameters are invalid")
            return MarketStreamRequest(
                symbols=symbols,
                after_sequence=after,
                heartbeat_seconds=heartbeat,
            )
        except AuthorizationError:
            return ApiResponse(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        except (ValueError, IndexError):
            return ApiResponse(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})

    def handle(
        self,
        *,
        method: str,
        target: str,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
    ) -> ApiResponse:
        if method not in {"GET", "POST", "DELETE"}:
            return ApiResponse(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})
        parsed = urlparse(target)
        parts = tuple(unquote(value) for value in parsed.path.strip("/").split("/") if value)
        query = parse_qs(parsed.query)
        if parts == ("health",):
            return ApiResponse(
                HTTPStatus.OK, {"api_version": API_VERSION, "status": "ready"}
            )
        if parts == ("ready",):
            return self._readiness()
        try:
            token = self._bearer(headers)
            if len(parts) == 4 and parts[:2] == (API_VERSION, "strategies"):
                if parts[3] != "signals":
                    return ApiResponse(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                strategy_id = parts[2]
                principal = self.registry.authorize(
                    token, scope=AccessScope.SIGNALS_READ, strategy_id=strategy_id
                )
                since_values = query.get("since")
                since = self._utc(since_values[0]) if since_values else None
                signals = self.registry.list_signals(strategy_id, since_utc=since)
                return ApiResponse(
                    HTTPStatus.OK,
                    {
                        "api_version": API_VERSION,
                        "principal_id": principal,
                        "signals": [signal.model_dump(mode="json") for signal in signals],
                    },
                )
            if len(parts) == 3 and parts[:2] == (API_VERSION, "features"):
                self.registry.authorize(token, scope=AccessScope.FEATURES_READ)
                asof_values = query.get("asof")
                if not asof_values:
                    raise ValueError("asof is required")
                asof = self._utc(asof_values[0])
                vector = self.feature_store.latest_vector(parts[2], asof_utc=asof)
                return ApiResponse(
                    HTTPStatus.OK,
                    {
                        "api_version": API_VERSION,
                        "feature_vector": (
                            None if vector is None else vector.model_dump(mode="json")
                        ),
                    },
                )
            if len(parts) == 3 and parts[:2] == (API_VERSION, "market-data"):
                if parts[2] == "subscriptions" and method == "POST":
                    principal = self.registry.authorize(
                        token, scope=AccessScope.MARKET_DATA_WRITE
                    )
                    if self.raw_store is None:
                        return ApiResponse(
                            HTTPStatus.SERVICE_UNAVAILABLE, {"error": "unavailable"}
                        )
                    request = MarketDataSubscriptionRequest.model_validate(body or {})
                    updated_at = self.clock()
                    if request.expires_at_utc <= updated_at:
                        raise ValueError("subscription already expired")
                    symbols = self.registry.set_market_subscription(
                        principal_id=principal,
                        symbols=request.symbols,
                        expires_at_utc=request.expires_at_utc,
                        updated_at_utc=updated_at,
                    )
                    return ApiResponse(
                        HTTPStatus.OK,
                        {
                            "api_version": API_VERSION,
                            "symbols": list(symbols),
                            "expires_at_utc": request.expires_at_utc.isoformat(),
                            "start_after_sequence": self.raw_store.sequence_before(
                                request.replay_from_utc
                            ),
                        },
                    )
                self.registry.authorize(token, scope=AccessScope.MARKET_DATA_READ)
                if parts[2] == "status" and method == "GET":
                    return self._market_status(query=query, now_utc=self.clock())
                if parts[2] == "news" and method == "GET":
                    if self.market_data is None:
                        return ApiResponse(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "unavailable"})
                    start = self._utc(query.get("start", [""])[0])
                    end = self._utc(query.get("end", [""])[0])
                    news = self.market_data.news(start_utc=start, end_utc=end)
                    return ApiResponse(
                        HTTPStatus.OK,
                        {"api_version": API_VERSION, "news": list(news)},
                    )
                symbols = self._symbols(query.get("symbols", [""])[0])
                if parts[2] == "events" and method == "GET":
                    if self.raw_store is None:
                        return ApiResponse(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "unavailable"})
                    after = int(query.get("after", ["0"])[0])
                    limit = int(query.get("limit", ["1000"])[0])
                    events = self.raw_store.list_after(
                        after_sequence=after, symbols=symbols, limit=limit
                    )
                    return ApiResponse(
                        HTTPStatus.OK,
                        {
                            "api_version": API_VERSION,
                            "events": [
                                {"sequence": sequence, "event": event.model_dump(mode="json")}
                                for sequence, event in events
                            ],
                            "next_sequence": events[-1][0] if events else after,
                        },
                    )
                if parts[2] in {"bars", "quotes"} and method == "GET":
                    if self.market_data is None:
                        return ApiResponse(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "unavailable"})
                    start = self._utc(query.get("start", [""])[0])
                    end = self._utc(query.get("end", [""])[0])
                    rows = (
                        self.market_data.bars(
                            symbols=symbols, start_utc=start, end_utc=end
                        )
                        if parts[2] == "bars"
                        else self.market_data.quotes(
                            symbols=symbols, start_utc=start, end_utc=end
                        )
                    )
                    coverage_kind: Literal["bars", "quotes"] = (
                        "bars" if parts[2] == "bars" else "quotes"
                    )
                    return ApiResponse(
                        HTTPStatus.OK,
                        {
                            "api_version": API_VERSION,
                            parts[2]: list(rows),
                            "coverage": build_historical_coverage(
                                kind=coverage_kind,
                                symbols=symbols,
                                start_utc=start,
                                end_utc=end,
                                rows=rows,
                                now_utc=self.clock(),
                            ),
                        },
                    )
            if len(parts) >= 3 and parts[:2] == (API_VERSION, "paper"):
                if self.paper_broker is None:
                    return ApiResponse(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "unavailable"})
                requested_scope = (
                    AccessScope.PAPER_READ if method == "GET" else AccessScope.PAPER_WRITE
                )
                self.registry.authorize(token, scope=requested_scope)
                if parts[2:] == ("account",) and method == "GET":
                    account = self.paper_broker.get_account()
                    return ApiResponse(
                        HTTPStatus.OK,
                        {"api_version": API_VERSION, "account": account.model_dump(mode="json")},
                    )
                if parts[2:] == ("positions",) and method == "GET":
                    positions = self.paper_broker.list_positions()
                    return ApiResponse(
                        HTTPStatus.OK,
                        {
                            "api_version": API_VERSION,
                            "positions": [item.model_dump(mode="json") for item in positions],
                        },
                    )
                if parts[2:] == ("orders", "open") and method == "GET":
                    orders = self.paper_broker.list_open_orders()
                    return ApiResponse(
                        HTTPStatus.OK,
                        {
                            "api_version": API_VERSION,
                            "orders": [item.model_dump(mode="json") for item in orders],
                        },
                    )
                if parts[2:] == ("orders", "by-client-id") and method == "GET":
                    client_order_id = query.get("client_order_id", [""])[0]
                    if not client_order_id:
                        raise ValueError("client_order_id is required")
                    order = self.paper_broker.get_order_by_client_id(client_order_id)
                    return ApiResponse(
                        HTTPStatus.OK,
                        {
                            "api_version": API_VERSION,
                            "order": None if order is None else order.model_dump(mode="json"),
                        },
                    )
                if parts[2:] == ("orders",) and method == "POST":
                    if body is None or not isinstance(body.get("request"), dict):
                        raise ValueError("order request is required")
                    request_body = body["request"]
                    if body.get("kind") == "entry":
                        order = self.paper_broker.submit_order_idempotent(
                            PaperOrderRequest.model_validate(request_body)
                        )
                    elif body.get("kind") == "close":
                        order = self.paper_broker.submit_close_order_idempotent(
                            PaperCloseRequest.model_validate(request_body)
                        )
                    else:
                        raise ValueError("order kind is invalid")
                    return ApiResponse(
                        HTTPStatus.OK,
                        {"api_version": API_VERSION, "order": order.model_dump(mode="json")},
                    )
                if len(parts) == 5 and parts[2:4] == ("orders", "cancel") and method == "DELETE":
                    cancelled = self.paper_broker.cancel_order(parts[4])
                    return ApiResponse(
                        HTTPStatus.OK,
                        {"api_version": API_VERSION, "cancelled": cancelled},
                    )
            return ApiResponse(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except AuthorizationError:
            return ApiResponse(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        except BrokerWritesDisabledError:
            return ApiResponse(HTTPStatus.FORBIDDEN, {"error": "writes_disabled"})
        except (BrokerError, MarketDataError):
            return ApiResponse(HTTPStatus.BAD_GATEWAY, {"error": "upstream_failed"})
        except (ValueError, IndexError):
            return ApiResponse(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})


def build_http_server(
    application: ApiApplication, *, host: str, port: int
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _handle(self, method: str) -> None:
            body: dict[str, object] | None = None
            if method == "POST":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 64 * 1024:
                        raise ValueError
                    parsed = json.loads(self.rfile.read(length))
                    if not isinstance(parsed, dict):
                        raise ValueError
                    body = parsed
                except (ValueError, json.JSONDecodeError):
                    response = ApiResponse(
                        HTTPStatus.BAD_REQUEST, {"error": "invalid_request"}
                    )
                    self._write(response)
                    return
            headers = {key.lower(): value for key, value in self.headers.items()}
            response = application.handle(
                method=method, target=self.path, headers=headers, body=body
            )
            self._write(response)

        def _write(self, response: ApiResponse) -> None:
            payload = json.dumps(response.body, ensure_ascii=False).encode()
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _stream_market_data(self) -> None:
            headers = {key.lower(): value for key, value in self.headers.items()}
            prepared = application.prepare_market_stream(
                target=self.path, headers=headers
            )
            if isinstance(prepared, ApiResponse):
                self._write(prepared)
                return
            store = application.raw_store
            if store is None:
                self._write(
                    ApiResponse(
                        HTTPStatus.SERVICE_UNAVAILABLE, {"error": "unavailable"}
                    )
                )
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            cursor = prepared.after_sequence
            next_heartbeat = time.monotonic()
            try:
                self.wfile.write(b"retry: 1000\n\n")
                self.wfile.flush()
                while True:
                    events = store.list_after(
                        after_sequence=cursor,
                        symbols=prepared.symbols,
                        limit=1000,
                    )
                    if events:
                        for sequence, event in events:
                            self.wfile.write(encode_sse_event(sequence, event))
                            cursor = sequence
                        self.wfile.flush()
                        next_heartbeat = (
                            time.monotonic() + prepared.heartbeat_seconds
                        )
                        continue
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        self.wfile.write(b": heartbeat\n\n")
                        self.wfile.flush()
                        next_heartbeat = now + prepared.heartbeat_seconds
                    time.sleep(0.25)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

        def do_GET(self) -> None:  # noqa: N802
            if urlparse(self.path).path == f"/{API_VERSION}/market-data/stream":
                self._stream_market_data()
            else:
                self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
