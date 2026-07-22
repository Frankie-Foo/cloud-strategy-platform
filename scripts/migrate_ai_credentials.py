"""Move Alpaca credentials to this service and issue scoped AI API tokens."""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from cloud_strategy_platform.contracts import AccessScope
from cloud_strategy_platform.registry import StrategyRegistry

ROOT = Path(__file__).resolve().parents[1]
ALPACA_KEYS = ("ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY")
REMOVE_FROM_AI = (
    *ALPACA_KEYS,
    "ALPACA_TRADING_BASE_URL",
    "ALPACA_MARKET_DATA_FEED",
)


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _values(lines: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in lines:
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _updated(
    lines: list[str], *, updates: dict[str, str], removals: tuple[str, ...] = ()
) -> str:
    remaining = dict(updates)
    output: list[str] = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in removals:
            continue
        if key in remaining:
            output.append(f"{key}={remaining.pop(key)}")
        else:
            output.append(line)
    if output and output[-1]:
        output.append("")
    output.extend(f"{key}={value}" for key, value in remaining.items())
    return "\n".join(output).rstrip() + "\n"


def _atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ai-env", type=Path, required=True)
    parser.add_argument("--cloud-env", type=Path, default=ROOT / ".env")
    parser.add_argument("--registry-db", type=Path, default=ROOT / "runs/platform.sqlite3")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()

    ai_lines = _lines(args.ai_env)
    ai_values = _values(ai_lines)
    cloud_lines = _lines(args.cloud_env)
    cloud_values = _values(cloud_lines)
    credentials = {
        key: ai_values.get(key, "") or cloud_values.get(key, "") for key in ALPACA_KEYS
    }
    if any(not value for value in credentials.values()):
        raise RuntimeError("Alpaca credentials are unavailable for migration")

    registry = StrategyRegistry(args.registry_db)
    market_token = registry.issue_token(
        principal_id="ai-investment-market", scope=AccessScope.MARKET_DATA_READ
    )
    paper_token = registry.issue_token(
        principal_id="ai-investment-paper", scope=AccessScope.PAPER_WRITE
    )
    feature_token = registry.issue_token(
        principal_id="ai-investment-features", scope=AccessScope.FEATURES_READ
    )
    _atomic(
        args.cloud_env,
        _updated(
            cloud_lines,
            updates={**credentials, "PAPER_BROKER_WRITE_ENABLED": "false"},
        ),
    )
    _atomic(
        args.ai_env,
        _updated(
            ai_lines,
            removals=REMOVE_FROM_AI,
            updates={
                "CLOUD_PLATFORM_BASE_URL": args.base_url.rstrip("/"),
                "CLOUD_MARKET_DATA_API_TOKEN": market_token,
                "CLOUD_PAPER_API_TOKEN": paper_token,
                "CLOUD_FEATURE_API_TOKEN": feature_token,
                "CLOUD_MARKET_DATA_FEED": "sip",
            },
        ),
    )
    print("AI credentials migrated to scoped cloud API tokens; secrets were not displayed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
