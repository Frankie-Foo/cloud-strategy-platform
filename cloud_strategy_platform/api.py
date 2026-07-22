"""Versioned HTTP boundary; raw SIP and order capabilities are intentionally absent."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from cloud_strategy_platform.contracts import API_VERSION, AccessScope
from cloud_strategy_platform.feature_store import SharedFeatureStore
from cloud_strategy_platform.registry import AuthorizationError, StrategyRegistry


@dataclass(frozen=True)
class ApiResponse:
    status: int
    body: dict[str, Any]


class ApiApplication:
    def __init__(self, *, registry: StrategyRegistry, feature_store: SharedFeatureStore):
        self.registry = registry
        self.feature_store = feature_store

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

    def handle(self, *, method: str, target: str, headers: dict[str, str]) -> ApiResponse:
        if method != "GET":
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
            return ApiResponse(HTTPStatus.NOT_FOUND, {"error": "not_found"})
        except AuthorizationError:
            return ApiResponse(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        except (ValueError, IndexError):
            return ApiResponse(HTTPStatus.BAD_REQUEST, {"error": "invalid_request"})


def build_http_server(
    application: ApiApplication, *, host: str, port: int
) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            headers = {key.lower(): value for key, value in self.headers.items()}
            response = application.handle(method="GET", target=self.path, headers=headers)
            payload = json.dumps(response.body, ensure_ascii=False).encode()
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    return ThreadingHTTPServer((host, port), Handler)
