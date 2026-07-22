"""Versioned HTTP boundary; raw SIP and order capabilities are intentionally absent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Protocol
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
    MarketDataSubscriptionRequest,
)
from cloud_strategy_platform.feature_store import RawSipEventStore, SharedFeatureStore
from cloud_strategy_platform.registry import AuthorizationError, StrategyRegistry


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: dict[str, Any]


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
    ):
        self.registry = registry
        self.feature_store = feature_store
        self.raw_store = raw_store
        self.market_data = market_data
        self.paper_broker = paper_broker

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
                    updated_at = datetime.now(UTC)
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
                    return ApiResponse(
                        HTTPStatus.OK,
                        {"api_version": API_VERSION, parts[2]: list(rows)},
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

        def do_GET(self) -> None:  # noqa: N802
            self._handle("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._handle("POST")

        def do_DELETE(self) -> None:  # noqa: N802
            self._handle("DELETE")

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
