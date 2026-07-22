from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cloud_strategy_platform.contracts import AccessScope
from cloud_strategy_platform.registry import AuthorizationError, StrategyRegistry
from scripts import migrate_ai_credentials


def _env(path: Path) -> dict[str, str]:
    return {
        key: value
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
        for key, value in (line.split("=", 1),)
    }


def test_migration_moves_provider_keys_and_issues_scoped_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ai_env = tmp_path / "ai.env"
    cloud_env = tmp_path / "cloud.env"
    registry_db = tmp_path / "platform.sqlite3"
    ai_env.write_text(
        "ALPACA_API_KEY_ID=provider-key\n"
        "ALPACA_API_SECRET_KEY=provider-secret\n"
        "BROKER_WRITE_ENABLED=false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "migrate",
            "--ai-env",
            str(ai_env),
            "--cloud-env",
            str(cloud_env),
            "--registry-db",
            str(registry_db),
        ],
    )
    assert migrate_ai_credentials.main() == 0
    ai = _env(ai_env)
    cloud = _env(cloud_env)
    assert "ALPACA_API_KEY_ID" not in ai
    assert "ALPACA_API_SECRET_KEY" not in ai
    assert cloud["ALPACA_API_KEY_ID"] == "provider-key"
    assert cloud["ALPACA_API_SECRET_KEY"] == "provider-secret"
    registry = StrategyRegistry(registry_db)
    registry.authorize(
        ai["CLOUD_MARKET_DATA_API_TOKEN"], scope=AccessScope.MARKET_DATA_WRITE
    )
    registry.authorize(ai["CLOUD_MARKET_DATA_API_TOKEN"], scope=AccessScope.MARKET_DATA_READ)
    registry.authorize(ai["CLOUD_PAPER_API_TOKEN"], scope=AccessScope.PAPER_READ)
    registry.authorize(ai["CLOUD_FEATURE_API_TOKEN"], scope=AccessScope.FEATURES_READ)

    first_market_token = ai["CLOUD_MARKET_DATA_API_TOKEN"]
    assert migrate_ai_credentials.main() == 0
    rotated = _env(ai_env)
    assert rotated["CLOUD_MARKET_DATA_API_TOKEN"] != first_market_token
    with pytest.raises(AuthorizationError):
        registry.authorize(first_market_token, scope=AccessScope.MARKET_DATA_READ)
    registry.authorize(
        rotated["CLOUD_MARKET_DATA_API_TOKEN"], scope=AccessScope.MARKET_DATA_WRITE
    )
