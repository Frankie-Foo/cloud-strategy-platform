"""Strategy catalog, isolated artifacts/signals, and hashed API credentials."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path

from cloud_strategy_platform.contracts import (
    AccessScope,
    ArtifactStage,
    DerivedSignal,
    MarketDataRuntime,
    MarketDataRuntimeState,
    SignalAction,
    StrategyArtifact,
    StrategyDefinition,
    require_utc,
    validate_strategy_id,
)
from cloud_strategy_platform.expressions import SafeExpression


class AuthorizationError(PermissionError):
    pass


class StrategyRegistry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategy_versions (
                    strategy_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    definition_sha256 TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0, 1)),
                    PRIMARY KEY (strategy_id, version)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_strategy_active
                    ON strategy_versions(strategy_id) WHERE active=1;
                CREATE TABLE IF NOT EXISTS strategy_artifacts (
                    strategy_id TEXT NOT NULL,
                    strategy_version TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, stage, trade_date, artifact_id),
                    FOREIGN KEY (strategy_id, strategy_version)
                        REFERENCES strategy_versions(strategy_id, version)
                );
                CREATE TABLE IF NOT EXISTS derived_signals (
                    strategy_id TEXT NOT NULL,
                    signal_id TEXT NOT NULL,
                    asof_utc TEXT NOT NULL,
                    signal_json TEXT NOT NULL,
                    PRIMARY KEY (strategy_id, signal_id)
                );
                CREATE TABLE IF NOT EXISTS api_tokens (
                    token_sha256 TEXT PRIMARY KEY,
                    principal_id TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    strategy_id TEXT,
                    created_at_utc TEXT NOT NULL,
                    revoked_at_utc TEXT
                );
                CREATE TABLE IF NOT EXISTS market_data_subscriptions (
                    principal_id TEXT PRIMARY KEY,
                    symbols_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    expires_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_market_data_subscription_expiry
                    ON market_data_subscriptions(expires_at_utc);
                CREATE TABLE IF NOT EXISTS market_data_runtime (
                    runtime_id INTEGER PRIMARY KEY CHECK(runtime_id=1),
                    runtime_json TEXT NOT NULL
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def register(
        self, definition: StrategyDefinition, *, activate: bool = False
    ) -> StrategyDefinition:
        if definition.expression is not None:
            SafeExpression(definition.expression)
        serialized = definition.model_dump_json()
        digest = hashlib.sha256(serialized.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT definition_json FROM strategy_versions WHERE strategy_id=? AND version=?",
                (definition.strategy_id, definition.version),
            ).fetchone()
            if row is not None and str(row[0]) != serialized:
                raise ValueError("strategy version is immutable")
            if row is None:
                connection.execute(
                    """
                    INSERT INTO strategy_versions (
                        strategy_id, version, definition_json, definition_sha256, active
                    ) VALUES (?, ?, ?, ?, 0)
                    """,
                    (definition.strategy_id, definition.version, serialized, digest),
                )
            if activate:
                connection.execute(
                    "UPDATE strategy_versions SET active=0 WHERE strategy_id=?",
                    (definition.strategy_id,),
                )
                connection.execute(
                    "UPDATE strategy_versions SET active=1 WHERE strategy_id=? AND version=?",
                    (definition.strategy_id, definition.version),
                )
            connection.commit()
        return definition

    def active_strategies(self) -> tuple[StrategyDefinition, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT definition_json FROM strategy_versions WHERE active=1 ORDER BY strategy_id"
            ).fetchall()
        return tuple(StrategyDefinition.model_validate_json(str(row[0])) for row in rows)

    def get_active(self, strategy_id: str) -> StrategyDefinition | None:
        validate_strategy_id(strategy_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT definition_json FROM strategy_versions WHERE strategy_id=? AND active=1",
                (strategy_id,),
            ).fetchone()
        return None if row is None else StrategyDefinition.model_validate_json(str(row[0]))

    def set_market_subscription(
        self,
        *,
        principal_id: str,
        symbols: tuple[str, ...],
        expires_at_utc: datetime,
        updated_at_utc: datetime,
    ) -> tuple[str, ...]:
        require_utc(updated_at_utc)
        require_utc(expires_at_utc)
        if not principal_id.strip() or expires_at_utc <= updated_at_utc:
            raise ValueError("market-data subscription lease is invalid")
        normalized = tuple(sorted({symbol.strip().upper() for symbol in symbols}))
        if not normalized or len(normalized) > 500 or any(
            re.fullmatch(r"[A-Z][A-Z0-9.-]{0,15}", symbol) is None
            for symbol in normalized
        ):
            raise ValueError("market-data subscription symbols are invalid")
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO market_data_subscriptions (
                    principal_id, symbols_json, updated_at_utc, expires_at_utc
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(principal_id) DO UPDATE SET
                    symbols_json=excluded.symbols_json,
                    updated_at_utc=excluded.updated_at_utc,
                    expires_at_utc=excluded.expires_at_utc
                """,
                (
                    principal_id.strip(),
                    json.dumps(normalized, separators=(",", ":")),
                    updated_at_utc.isoformat(),
                    expires_at_utc.isoformat(),
                ),
            )
        return normalized

    def active_market_symbols(self, *, at_utc: datetime) -> tuple[str, ...]:
        require_utc(at_utc)
        symbols = {
            symbol
            for definition in self.active_strategies()
            for symbol in definition.symbols
        }
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT symbols_json FROM market_data_subscriptions
                WHERE expires_at_utc>?
                """,
                (at_utc.isoformat(),),
            ).fetchall()
        for row in rows:
            value = json.loads(str(row[0]))
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError("stored market-data subscription is invalid")
            symbols.update(value)
        return tuple(sorted(symbols))

    def record_market_runtime(
        self,
        *,
        state: MarketDataRuntimeState,
        symbols: tuple[str, ...],
        process_started_at_utc: datetime,
        heartbeat_at_utc: datetime,
        connected_at_utc: datetime | None,
        last_event_at_utc: datetime | None,
        last_error_code: str | None,
        last_error_at_utc: datetime | None,
        reconnect_count: int,
    ) -> MarketDataRuntime:
        runtime = MarketDataRuntime(
            state=state,
            symbols=symbols,
            process_started_at_utc=process_started_at_utc,
            heartbeat_at_utc=heartbeat_at_utc,
            connected_at_utc=connected_at_utc,
            last_event_at_utc=last_event_at_utc,
            last_error_code=last_error_code,
            last_error_at_utc=last_error_at_utc,
            reconnect_count=reconnect_count,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO market_data_runtime (runtime_id, runtime_json)
                VALUES (1, ?)
                ON CONFLICT(runtime_id) DO UPDATE SET
                    runtime_json=excluded.runtime_json
                """,
                (runtime.model_dump_json(),),
            )
        return runtime

    def market_runtime(self) -> MarketDataRuntime | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT runtime_json FROM market_data_runtime WHERE runtime_id=1"
            ).fetchone()
        if row is None:
            return None
        return MarketDataRuntime.model_validate_json(str(row[0]))

    def record_artifact(self, artifact: StrategyArtifact) -> StrategyArtifact:
        serialized = artifact.model_dump_json()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT artifact_json FROM strategy_artifacts
                WHERE strategy_id=? AND stage=? AND trade_date=? AND artifact_id=?
                """,
                (
                    artifact.strategy_id,
                    artifact.stage.value,
                    artifact.trade_date.isoformat(),
                    artifact.artifact_id,
                ),
            ).fetchone()
            if row is not None and str(row[0]) != serialized:
                raise ValueError("artifact identity belongs to different content")
            if row is None:
                connection.execute(
                    """
                    INSERT INTO strategy_artifacts (
                        strategy_id, strategy_version, stage, trade_date,
                        artifact_id, artifact_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact.strategy_id,
                        artifact.strategy_version,
                        artifact.stage.value,
                        artifact.trade_date.isoformat(),
                        artifact.artifact_id,
                        serialized,
                    ),
                )
            connection.commit()
        return artifact

    def list_artifacts(
        self,
        strategy_id: str,
        *,
        stage: ArtifactStage | None = None,
        trade_date: date | None = None,
    ) -> tuple[StrategyArtifact, ...]:
        validate_strategy_id(strategy_id)
        clauses = ["strategy_id=?"]
        values = [strategy_id]
        if stage is not None:
            clauses.append("stage=?")
            values.append(stage.value)
        if trade_date is not None:
            clauses.append("trade_date=?")
            values.append(trade_date.isoformat())
        query = (
            "SELECT artifact_json FROM strategy_artifacts WHERE "
            + " AND ".join(clauses)
            + " ORDER BY trade_date, artifact_id"
        )
        with self._connect() as connection:
            rows = connection.execute(query, values).fetchall()
        return tuple(StrategyArtifact.model_validate_json(str(row[0])) for row in rows)

    def publish_signal(
        self,
        *,
        strategy_id: str,
        strategy_version: str,
        symbol: str,
        asof_utc: datetime,
        action: SignalAction,
        reason: str,
        feature_provenance: tuple[str, ...],
    ) -> DerivedSignal:
        identity = json.dumps(
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "symbol": symbol,
                "asof_utc": asof_utc.isoformat(),
                "action": action.value,
            },
            sort_keys=True,
        )
        signal = DerivedSignal(
            signal_id="signal-" + hashlib.sha256(identity.encode()).hexdigest()[:24],
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            symbol=symbol,
            asof_utc=asof_utc,
            action=action,
            reason=reason,
            feature_provenance=feature_provenance,
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO derived_signals (
                    strategy_id, signal_id, asof_utc, signal_json
                ) VALUES (?, ?, ?, ?)
                """,
                (strategy_id, signal.signal_id, asof_utc.isoformat(), signal.model_dump_json()),
            )
        return signal

    def list_signals(
        self, strategy_id: str, *, since_utc: datetime | None = None
    ) -> tuple[DerivedSignal, ...]:
        validate_strategy_id(strategy_id)
        with self._connect() as connection:
            if since_utc is None:
                rows = connection.execute(
                    """
                    SELECT signal_json FROM derived_signals
                    WHERE strategy_id=? ORDER BY asof_utc, signal_id
                    """,
                    (strategy_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT signal_json FROM derived_signals
                    WHERE strategy_id=? AND asof_utc>=? ORDER BY asof_utc, signal_id
                    """,
                    (strategy_id, since_utc.isoformat()),
                ).fetchall()
        return tuple(DerivedSignal.model_validate_json(str(row[0])) for row in rows)

    def issue_token(
        self,
        *,
        principal_id: str,
        scope: AccessScope,
        strategy_id: str | None = None,
    ) -> str:
        if not principal_id.strip():
            raise ValueError("principal_id is required")
        if scope is AccessScope.SIGNALS_READ:
            if strategy_id is None:
                raise ValueError("signal tokens require strategy_id")
            validate_strategy_id(strategy_id)
        elif strategy_id is not None:
            raise ValueError("feature-service tokens cannot be strategy grants")
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO api_tokens (
                    token_sha256, principal_id, scope, strategy_id, created_at_utc
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (digest, principal_id, scope.value, strategy_id, datetime.now(UTC).isoformat()),
            )
        return token

    def authorize(
        self, token: str, *, scope: AccessScope, strategy_id: str | None = None
    ) -> str:
        if not token.strip():
            raise AuthorizationError("missing bearer token")
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT principal_id, scope, strategy_id FROM api_tokens
                WHERE token_sha256=? AND revoked_at_utc IS NULL
                """,
                (digest,),
            ).fetchone()
        if row is None:
            raise AuthorizationError("token scope is not authorized")
        granted_scope = AccessScope(str(row["scope"]))
        allowed = granted_scope is scope or (
            granted_scope is AccessScope.PAPER_WRITE and scope is AccessScope.PAPER_READ
        ) or (
            granted_scope is AccessScope.MARKET_DATA_WRITE
            and scope is AccessScope.MARKET_DATA_READ
        )
        if not allowed:
            raise AuthorizationError("token scope is not authorized")
        granted_strategy = None if row["strategy_id"] is None else str(row["strategy_id"])
        if scope is AccessScope.SIGNALS_READ and granted_strategy != strategy_id:
            raise AuthorizationError("token is not authorized for this strategy")
        return str(row["principal_id"])

    def revoke_other_tokens(
        self,
        *,
        principal_id: str,
        active_token: str,
        revoked_at_utc: datetime | None = None,
    ) -> int:
        if not principal_id.strip() or not active_token.strip():
            raise ValueError("principal_id and active_token are required")
        revoked_at = datetime.now(UTC) if revoked_at_utc is None else require_utc(revoked_at_utc)
        active_digest = hashlib.sha256(active_token.encode()).hexdigest()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE api_tokens SET revoked_at_utc=?
                WHERE principal_id=? AND token_sha256<>? AND revoked_at_utc IS NULL
                """,
                (revoked_at.isoformat(), principal_id, active_digest),
            )
        return cursor.rowcount
