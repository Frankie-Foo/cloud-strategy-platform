"""Single raw SIP ingestion into shared features and strategy-scoped signals."""

from __future__ import annotations

from cloud_strategy_platform.contracts import (
    DerivedSignal,
    FeatureVector,
    SignalAction,
    StrategyDefinition,
    StrategyKind,
)
from cloud_strategy_platform.expressions import ExpressionEvaluationError, SafeExpression
from cloud_strategy_platform.feature_store import (
    PointInTimeFeatureLibrary,
    RawSipEventStore,
    SharedFeatureStore,
)
from cloud_strategy_platform.market_data import SipEvent
from cloud_strategy_platform.registry import StrategyRegistry
from cloud_strategy_platform.sandbox import PythonSandboxError, PythonStrategyRunner


class CloudStrategyPlatform:
    def __init__(
        self,
        *,
        registry: StrategyRegistry,
        raw_store: RawSipEventStore,
        feature_store: SharedFeatureStore,
        python_runner: PythonStrategyRunner | None = None,
    ):
        self.registry = registry
        self.raw_store = raw_store
        self.feature_library = PointInTimeFeatureLibrary(feature_store)
        self.python_runner = python_runner

    def _evaluate(
        self, definition: StrategyDefinition, vector: FeatureVector
    ) -> tuple[SignalAction, str] | None:
        if definition.kind is StrategyKind.PYTHON_SANDBOX:
            if self.python_runner is None:
                return None
            try:
                return self.python_runner.evaluate(definition, vector)
            except PythonSandboxError:
                return None
        context = dict(vector.values)
        if context.keys() & definition.parameters.keys():
            return None
        context.update(definition.parameters)
        try:
            matched = SafeExpression(definition.expression or "").evaluate(context)
        except ExpressionEvaluationError:
            return None
        if matched:
            return SignalAction.ENTER_LONG, "safe expression matched"
        return SignalAction.WATCH, "safe expression did not match"

    def process_sip_event(self, event: SipEvent) -> tuple[DerivedSignal, ...]:
        event_id = self.raw_store.append(event)
        vector = self.feature_library.ingest(event, event_id=event_id)
        provenance = tuple(feature.provenance for feature in vector.features)
        signals: list[DerivedSignal] = []
        for definition in self.registry.active_strategies():
            if event.symbol not in definition.symbols:
                continue
            decision = self._evaluate(definition, vector)
            if decision is None:
                continue
            action, reason = decision
            signals.append(
                self.registry.publish_signal(
                    strategy_id=definition.strategy_id,
                    strategy_version=definition.version,
                    symbol=event.symbol,
                    asof_utc=event.ts_utc,
                    action=action,
                    reason=reason,
                    feature_provenance=provenance,
                )
            )
        return tuple(signals)
