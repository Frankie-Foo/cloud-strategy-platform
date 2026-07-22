from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

API_VERSION = "v1"
DEFAULT_STRATEGY_ID = "default"
STRATEGY_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
SHA256_PATTERN = r"^[0-9a-f]{64}$"
Scalar = bool | int | float | str


def validate_strategy_id(value: str) -> str:
    if not STRATEGY_ID_PATTERN.fullmatch(value):
        raise ValueError("strategy_id must match ^[a-z][a-z0-9_-]{0,63}$")
    return value


def require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be timezone-aware UTC")
    return value


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class StrategyKind(StrEnum):
    PARAMETERIZED = "parameterized"
    SAFE_EXPRESSION = "safe_expression"
    PYTHON_SANDBOX = "python_sandbox"


class ArtifactStage(StrEnum):
    CONFIG = "config"
    SELECTION = "selection"
    BACKTEST = "backtest"
    PAPER = "paper"
    REVIEW = "review"


class SignalAction(StrEnum):
    WATCH = "watch"
    ENTER_LONG = "enter_long"
    EXIT_LONG = "exit_long"


class AccessScope(StrEnum):
    SIGNALS_READ = "signals:read"
    FEATURES_READ = "features:read"


class StrategyDefinition(FrozenModel):
    strategy_id: str
    version: str = Field(min_length=1, max_length=64)
    kind: StrategyKind
    symbols: tuple[str, ...] = Field(min_length=1)
    parameters: dict[str, Scalar] = Field(default_factory=dict)
    expression: str | None = Field(default=None, max_length=4096)
    python_entrypoint: str | None = Field(default=None, max_length=128)
    created_at_utc: datetime
    created_by: str = Field(min_length=1, max_length=128)

    @field_validator("strategy_id")
    @classmethod
    def valid_strategy_id(cls, value: str) -> str:
        return validate_strategy_id(value)

    @field_validator("symbols")
    @classmethod
    def normalize_symbols(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({value.strip().upper() for value in values}))
        if not normalized or any(not value for value in normalized):
            raise ValueError("at least one non-empty symbol is required")
        return normalized

    @field_validator("created_at_utc")
    @classmethod
    def utc_created_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def validate_execution_contract(self) -> StrategyDefinition:
        declarative = {StrategyKind.PARAMETERIZED, StrategyKind.SAFE_EXPRESSION}
        if self.kind in declarative:
            if not self.expression:
                raise ValueError("declarative strategies require an expression")
            if self.python_entrypoint is not None:
                raise ValueError("declarative strategies cannot define Python")
        elif not self.python_entrypoint or self.expression is not None:
            raise ValueError("Python strategies require only a sandbox entrypoint")
        return self


class FeatureValue(FrozenModel):
    name: str = Field(min_length=1, max_length=128)
    value: Scalar | None
    asof_utc: datetime
    definition_version: str = Field(min_length=1, max_length=64)
    provenance: str = Field(min_length=1)

    @field_validator("asof_utc")
    @classmethod
    def utc_asof(cls, value: datetime) -> datetime:
        return require_utc(value)


class FeatureVector(FrozenModel):
    symbol: str = Field(min_length=1)
    asof_utc: datetime
    input_event_id: str = Field(min_length=1)
    features: tuple[FeatureValue, ...] = Field(min_length=1)

    @field_validator("asof_utc")
    @classmethod
    def utc_asof(cls, value: datetime) -> datetime:
        return require_utc(value)

    @property
    def values(self) -> dict[str, Scalar | None]:
        return {feature.name: feature.value for feature in self.features}


class DerivedSignal(FrozenModel):
    signal_id: str = Field(min_length=1)
    strategy_id: str
    strategy_version: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    asof_utc: datetime
    action: SignalAction
    reason: str = Field(min_length=1, max_length=512)
    feature_provenance: tuple[str, ...] = Field(min_length=1)

    @field_validator("strategy_id")
    @classmethod
    def valid_strategy_id(cls, value: str) -> str:
        return validate_strategy_id(value)

    @field_validator("asof_utc")
    @classmethod
    def utc_asof(cls, value: datetime) -> datetime:
        return require_utc(value)


class StrategyArtifact(FrozenModel):
    strategy_id: str
    strategy_version: str = Field(min_length=1)
    stage: ArtifactStage
    trade_date: date
    artifact_id: str = Field(min_length=1)
    uri: str = Field(pattern=r"^strategy://[a-z][a-z0-9_-]{0,63}/")
    content_sha256: str = Field(pattern=SHA256_PATTERN)
    created_at_utc: datetime

    @field_validator("strategy_id")
    @classmethod
    def valid_strategy_id(cls, value: str) -> str:
        return validate_strategy_id(value)

    @field_validator("created_at_utc")
    @classmethod
    def utc_created_at(cls, value: datetime) -> datetime:
        return require_utc(value)

    @model_validator(mode="after")
    def uri_matches_strategy(self) -> StrategyArtifact:
        if not self.uri.startswith(f"strategy://{self.strategy_id}/"):
            raise ValueError("artifact URI must be scoped to strategy_id")
        return self
