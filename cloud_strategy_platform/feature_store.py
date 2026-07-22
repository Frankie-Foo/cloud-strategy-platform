"""Raw SIP persistence and the versioned point-in-time feature library."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from cloud_strategy_platform.contracts import FeatureValue, FeatureVector, require_utc
from cloud_strategy_platform.market_data import SipBar, SipEvent, SipQuote


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    connection.execute("PRAGMA busy_timeout=30000")
    return connection


class RawSipEventStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS raw_sip_event_log (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE,
                    symbol TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    ts_utc TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    provenance TEXT NOT NULL
                )
                """
            )

    def append(self, event: SipEvent) -> str:
        serialized = event.model_dump_json()
        event_id = hashlib.sha256(serialized.encode()).hexdigest()
        with _connect(self.path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO raw_sip_event_log (
                    event_id, symbol, event_type, ts_utc, event_json, provenance
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event.symbol,
                    event.event_type,
                    event.ts_utc.isoformat(),
                    serialized,
                    event.provenance,
                ),
            )
        return event_id

    def count(self) -> int:
        with _connect(self.path) as connection:
            return int(
                connection.execute("SELECT COUNT(*) FROM raw_sip_event_log").fetchone()[0]
            )

    def list_after(
        self,
        *,
        after_sequence: int,
        symbols: tuple[str, ...],
        limit: int,
    ) -> tuple[tuple[int, SipEvent], ...]:
        if after_sequence < 0 or not symbols or not 1 <= limit <= 10_000:
            raise ValueError("event query is invalid")
        normalized = tuple(sorted({symbol.strip().upper() for symbol in symbols if symbol.strip()}))
        if not normalized:
            raise ValueError("at least one symbol is required")
        placeholders = ",".join("?" for _ in normalized)
        query = (
            "SELECT sequence, event_type, event_json FROM raw_sip_event_log "
            f"WHERE sequence>? AND symbol IN ({placeholders}) "
            "ORDER BY sequence LIMIT ?"
        )
        with _connect(self.path) as connection:
            rows = connection.execute(
                query, (after_sequence, *normalized, limit)
            ).fetchall()
        events: list[tuple[int, SipEvent]] = []
        for row in rows:
            model = SipBar if str(row["event_type"]) == "bar" else SipQuote
            events.append((int(row["sequence"]), model.model_validate_json(str(row["event_json"]))))
        return tuple(events)


class SharedFeatureStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with _connect(self.path) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS feature_values (
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    asof_utc TEXT NOT NULL,
                    definition_version TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    provenance TEXT NOT NULL,
                    input_event_id TEXT NOT NULL,
                    PRIMARY KEY (symbol, name, asof_utc, definition_version)
                );
                CREATE INDEX IF NOT EXISTS ix_feature_point_in_time
                    ON feature_values(symbol, asof_utc, name);
                """
            )

    def put(self, symbol: str, feature: FeatureValue, *, input_event_id: str) -> None:
        normalized = symbol.strip().upper()
        if not normalized or not input_event_id.strip():
            raise ValueError("symbol and input_event_id are required")
        with _connect(self.path) as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO feature_values (
                    symbol, name, asof_utc, definition_version, value_json,
                    provenance, input_event_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized,
                    feature.name,
                    feature.asof_utc.isoformat(),
                    feature.definition_version,
                    json.dumps(feature.value, ensure_ascii=False),
                    feature.provenance,
                    input_event_id,
                ),
            )

    def latest_vector(self, symbol: str, *, asof_utc: datetime) -> FeatureVector | None:
        require_utc(asof_utc)
        normalized = symbol.strip().upper()
        with _connect(self.path) as connection:
            rows = connection.execute(
                """
                SELECT name, value_json, asof_utc, definition_version, provenance,
                       input_event_id
                FROM (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY name ORDER BY asof_utc DESC, definition_version DESC
                    ) AS position
                    FROM feature_values WHERE symbol=? AND asof_utc<=?
                ) WHERE position=1 ORDER BY name
                """,
                (normalized, asof_utc.isoformat()),
            ).fetchall()
        if not rows:
            return None
        features = tuple(
            FeatureValue(
                name=str(row["name"]),
                value=json.loads(str(row["value_json"])),
                asof_utc=datetime.fromisoformat(str(row["asof_utc"])),
                definition_version=str(row["definition_version"]),
                provenance=str(row["provenance"]),
            )
            for row in rows
        )
        event_ids = sorted({str(row["input_event_id"]) for row in rows})
        return FeatureVector(
            symbol=normalized,
            asof_utc=asof_utc,
            input_event_id=hashlib.sha256("|".join(event_ids).encode()).hexdigest(),
            features=features,
        )


class PointInTimeFeatureLibrary:
    DEFINITION_VERSION = "sip.minute.v1"

    def __init__(self, store: SharedFeatureStore):
        self.store = store

    def ingest(self, event: SipEvent, *, event_id: str) -> FeatureVector:
        previous = self.store.latest_vector(event.symbol, asof_utc=event.ts_utc)
        if isinstance(event, SipBar):
            values: dict[str, float | int | None] = {
                "close": event.close,
                "volume": event.volume,
                "vwap": event.vwap,
                "minute_return": None,
            }
            previous_close = None if previous is None else previous.values.get("close")
            if isinstance(previous_close, (int, float)) and previous_close > 0:
                values["minute_return"] = event.close / previous_close - 1.0
        else:
            midpoint = (event.bid_price + event.ask_price) / 2
            values = {
                "nbbo_midpoint": midpoint,
                "nbbo_spread_bps": (
                    (event.ask_price - event.bid_price) / midpoint * 10_000
                    if midpoint > 0
                    else None
                ),
            }
        for name, value in values.items():
            self.store.put(
                event.symbol,
                FeatureValue(
                    name=name,
                    value=value,
                    asof_utc=event.ts_utc,
                    definition_version=self.DEFINITION_VERSION,
                    provenance=f"{event.provenance}|feature:{name}@{self.DEFINITION_VERSION}",
                ),
                input_event_id=event_id,
            )
        vector = self.store.latest_vector(event.symbol, asof_utc=event.ts_utc)
        if vector is None:
            raise RuntimeError("feature vector was not persisted")
        return vector
