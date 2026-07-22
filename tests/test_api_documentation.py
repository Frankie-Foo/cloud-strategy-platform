from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OPENAPI_PATH = ROOT / "docs" / "openapi-v1.json"
HTTP_METHODS = {"get", "post", "put", "patch", "delete"}

EXPECTED_SCOPES = {
    ("GET", "/health"): None,
    ("GET", "/v1/strategies/{strategy_id}/signals"): "signals:read",
    ("GET", "/v1/features/{symbol}"): "features:read",
    ("GET", "/v1/market-data/events"): "market-data:read",
    ("GET", "/v1/market-data/bars"): "market-data:read",
    ("GET", "/v1/market-data/quotes"): "market-data:read",
    ("GET", "/v1/market-data/news"): "market-data:read",
    ("GET", "/v1/paper/account"): "paper:read",
    ("GET", "/v1/paper/positions"): "paper:read",
    ("GET", "/v1/paper/orders/open"): "paper:read",
    ("GET", "/v1/paper/orders/by-client-id"): "paper:read",
    ("POST", "/v1/paper/orders"): "paper:write",
    ("DELETE", "/v1/paper/orders/cancel/{order_id}"): "paper:write",
}


def _document() -> dict[str, Any]:
    value = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _operations(document: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, path_item in document["paths"].items():
        for method, operation in path_item.items():
            if method in HTTP_METHODS:
                yield method.upper(), path, operation


def _refs(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "$ref" and isinstance(item, str):
                yield item
            else:
                yield from _refs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _refs(item)


def _resolve_local_ref(document: dict[str, Any], ref: str) -> object:
    assert ref.startswith("#/"), f"external reference is not allowed: {ref}"
    current: object = document
    for raw_part in ref.removeprefix("#/").split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        assert isinstance(current, dict)
        assert part in current, f"unresolved OpenAPI reference: {ref}"
        current = current[part]
    return current


def test_openapi_has_exactly_the_implemented_operations_and_scopes() -> None:
    document = _document()
    operations = {(method, path): operation for method, path, operation in _operations(document)}

    assert document["openapi"] == "3.1.0"
    assert set(operations) == set(EXPECTED_SCOPES)
    for key, expected_scope in EXPECTED_SCOPES.items():
        operation = operations[key]
        assert operation.get("x-required-scope") == expected_scope
        assert "200" in operation["responses"]
        if expected_scope is None:
            assert operation["security"] == []
        else:
            assert operation["security"] == [{"bearerAuth": []}]
            assert "401" in operation["responses"]


def test_openapi_write_operations_declare_both_scope_and_write_gate_error() -> None:
    document = _document()
    operations = {(method, path): operation for method, path, operation in _operations(document)}

    for key in (
        ("POST", "/v1/paper/orders"),
        ("DELETE", "/v1/paper/orders/cancel/{order_id}"),
    ):
        operation = operations[key]
        assert operation["x-required-scope"] == "paper:write"
        assert "403" in operation["responses"]


def test_all_openapi_references_resolve_and_error_responses_are_no_store() -> None:
    document = _document()

    for ref in _refs(document):
        _resolve_local_ref(document, ref)
    for response in document["components"]["responses"].values():
        assert response["headers"]["Cache-Control"]["$ref"] == (
            "#/components/headers/CacheControl"
        )


def test_public_contract_contains_no_forbidden_route_or_secret() -> None:
    document = _document()
    paths = set(document["paths"])
    serialized = json.dumps(document, ensure_ascii=False).lower()

    assert all("proxy" not in path and "alpaca" not in path for path in paths)
    assert all("live" not in path and "short" not in path for path in paths)
    assert "apca-api-key-id" not in serialized
    assert "apca-api-secret-key" not in serialized
    assert "alpaca_api_secret_key" not in serialized


def test_human_document_covers_every_current_route_and_labels_target_as_draft() -> None:
    current = (ROOT / "docs" / "API.md").read_text(encoding="utf-8")
    target = (ROOT / "docs" / "PLATFORM_API_TARGET.md").read_text(encoding="utf-8")

    for _, path in EXPECTED_SCOPES:
        assert path in current
    assert "尚未按策略隔离" in current
    assert "设计稿 / 尚未实现 / 不可调用" in target
    for required_family in (
        "selection-runs",
        "backtests",
        "paper-sessions",
        "reviews",
        "python-packages",
        "audit-events",
    ):
        assert required_family in target
