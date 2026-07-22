from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path

from cloud_strategy_platform.contracts import ArtifactStage, StrategyArtifact
from cloud_strategy_platform.registry import StrategyRegistry
from cloud_strategy_platform.scope import StrategyWorkspace


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


class StrategyArtifactRecorder:
    def __init__(self, *, registry: StrategyRegistry, workspace: StrategyWorkspace):
        self.registry = registry
        self.workspace = workspace

    def record_file(
        self, *, stage: ArtifactStage, trade_date: date, artifact_id: str, path: Path
    ) -> StrategyArtifact:
        definition = self.registry.get_active(self.workspace.strategy_id)
        if definition is None:
            raise ValueError("strategy has no active version")
        resolved = path.resolve(strict=True)
        allowed = (self.workspace.data_root.resolve(), self.workspace.runs_root.resolve())
        if not any(_is_within(resolved, root) for root in allowed):
            raise PermissionError("strategy artifact is outside its isolated workspace")
        return self.registry.record_artifact(
            StrategyArtifact(
                strategy_id=definition.strategy_id,
                strategy_version=definition.version,
                stage=stage,
                trade_date=trade_date,
                artifact_id=artifact_id,
                uri=self.workspace.artifact_uri(stage.value, artifact_id),
                content_sha256=_sha256_file(resolved),
                created_at_utc=datetime.now(UTC),
            )
        )
