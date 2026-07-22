from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from cloud_strategy_platform.api import ApiApplication
from cloud_strategy_platform.contracts import (
    AccessScope,
    SignalAction,
    StrategyDefinition,
    StrategyKind,
)
from cloud_strategy_platform.expressions import ExpressionRejectedError, SafeExpression
from cloud_strategy_platform.feature_store import RawSipEventStore, SharedFeatureStore
from cloud_strategy_platform.market_data import SipBar
from cloud_strategy_platform.platform import CloudStrategyPlatform
from cloud_strategy_platform.registry import StrategyRegistry
from cloud_strategy_platform.sandbox import DockerSandboxPolicy
from cloud_strategy_platform.scope import StrategyWorkspace

NOW = datetime(2026, 7, 22, 15, 10, tzinfo=UTC)


def _definition(strategy_id: str, threshold: float) -> StrategyDefinition:
    return StrategyDefinition(
        strategy_id=strategy_id,
        version="1.0.0",
        kind=StrategyKind.PARAMETERIZED,
        symbols=("AAPL",),
        parameters={"threshold": threshold},
        expression="minute_return > threshold and volume > 0",
        created_at_utc=NOW,
        created_by="owner",
    )


def _bar(ts_utc: datetime, close: float, provenance: str) -> SipBar:
    return SipBar(
        symbol="AAPL",
        ts_utc=ts_utc,
        open=100,
        high=max(100, close),
        low=min(100, close),
        close=close,
        volume=100,
        trade_count=5,
        vwap=close,
        provenance=provenance,
    )


def test_strategy_isolation_and_single_raw_ingestion(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    registry.register(_definition("alpha", 0.005), activate=True)
    registry.register(_definition("beta", 0.02), activate=True)
    raw = RawSipEventStore(tmp_path / "raw.sqlite3")
    platform = CloudStrategyPlatform(
        registry=registry,
        raw_store=raw,
        feature_store=SharedFeatureStore(tmp_path / "features.sqlite3"),
    )
    platform.process_sip_event(_bar(NOW - timedelta(minutes=1), 100, "sip@test-1"))
    signals = platform.process_sip_event(_bar(NOW, 101, "sip@test-2"))

    assert raw.count() == 2
    assert {signal.strategy_id for signal in signals} == {"alpha", "beta"}
    assert {signal.strategy_id: signal.action for signal in signals} == {
        "alpha": SignalAction.ENTER_LONG,
        "beta": SignalAction.WATCH,
    }


def test_http_scopes_separate_ai_features_from_collaborator_signals(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    registry.register(_definition("alpha", 0.005), activate=True)
    features = SharedFeatureStore(tmp_path / "features.sqlite3")
    platform = CloudStrategyPlatform(
        registry=registry,
        raw_store=RawSipEventStore(tmp_path / "raw.sqlite3"),
        feature_store=features,
    )
    platform.process_sip_event(_bar(NOW - timedelta(minutes=1), 100, "sip@test-1"))
    platform.process_sip_event(_bar(NOW, 101, "sip@test-2"))
    signal_token = registry.issue_token(
        principal_id="alice", scope=AccessScope.SIGNALS_READ, strategy_id="alpha"
    )
    feature_token = registry.issue_token(
        principal_id="ai-quant", scope=AccessScope.FEATURES_READ
    )
    api = ApiApplication(registry=registry, feature_store=features)

    signal_response = api.handle(
        method="GET",
        target="/v1/strategies/alpha/signals",
        headers={"authorization": f"Bearer {signal_token}"},
    )
    assert signal_response.status == 200
    assert len(signal_response.body["signals"]) == 1
    denied = api.handle(
        method="GET",
        target=f"/v1/features/AAPL?asof={NOW.isoformat()}",
        headers={"authorization": f"Bearer {signal_token}"},
    )
    assert denied.status == 401
    allowed = api.handle(
        method="GET",
        target=f"/v1/features/AAPL?asof={NOW.isoformat()}",
        headers={"authorization": f"Bearer {feature_token}"},
    )
    assert allowed.status == 200
    assert allowed.body["feature_vector"] is not None

    for unavailable in ("/v1/raw/AAPL", "/v1/orders", "/v1/proxy"):
        response = api.handle(
            method="GET",
            target=unavailable,
            headers={"authorization": f"Bearer {feature_token}"},
        )
        assert response.status == 404


def test_signal_token_cannot_cross_strategy_boundary(tmp_path: Path) -> None:
    registry = StrategyRegistry(tmp_path / "registry.sqlite3")
    registry.register(_definition("alpha", 0.005), activate=True)
    registry.register(_definition("beta", 0.02), activate=True)
    token = registry.issue_token(
        principal_id="alice", scope=AccessScope.SIGNALS_READ, strategy_id="alpha"
    )
    response = ApiApplication(
        registry=registry,
        feature_store=SharedFeatureStore(tmp_path / "features.sqlite3"),
    ).handle(
        method="GET",
        target="/v1/strategies/beta/signals",
        headers={"authorization": f"Bearer {token}"},
    )
    assert response.status == 401


def test_safe_expression_rejects_arbitrary_code() -> None:
    assert SafeExpression("price > threshold").evaluate({"price": 2, "threshold": 1})
    for source in ("__import__('os')", "price.__class__", "open('secret')"):
        with pytest.raises(ExpressionRejectedError):
            SafeExpression(source)


def test_strategy_workspace_has_no_legacy_default_escape(tmp_path: Path) -> None:
    alpha = StrategyWorkspace(tmp_path, "alpha")
    beta = StrategyWorkspace(tmp_path, "beta")
    assert alpha.data_root == tmp_path / "data/strategies/alpha"
    assert alpha.data_root != beta.data_root
    with pytest.raises(ValueError):
        StrategyWorkspace(tmp_path, "../escape")


def test_python_policy_has_hard_container_isolation() -> None:
    policy = DockerSandboxPolicy(image="sandbox@sha256:" + "a" * 64)
    command = policy.command(source_path=Path("strategy.py"), input_path=Path("input.json"))
    assert "--network=none" in command
    assert "--read-only" in command
    assert "--cap-drop=ALL" in command
    assert "--security-opt=no-new-privileges" in command
    assert not any("ALPACA" in argument or "BROKER" in argument for argument in command)


def test_cloud_repository_has_no_quant_or_broker_imports() -> None:
    root = Path(__file__).resolve().parents[1] / "cloud_strategy_platform"
    text = "\n".join(path.read_text(encoding="utf-8") for path in root.glob("*.py"))
    assert "from kernel" not in text
    assert "from execution" not in text
    assert "import kernel" not in text
    assert "import execution" not in text
