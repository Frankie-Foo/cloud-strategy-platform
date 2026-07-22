from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cloud_strategy_platform.contracts import validate_strategy_id


@dataclass(frozen=True)
class StrategyWorkspace:
    platform_root: Path
    strategy_id: str

    def __post_init__(self) -> None:
        validate_strategy_id(self.strategy_id)

    @property
    def data_root(self) -> Path:
        return self.platform_root / "data" / "strategies" / self.strategy_id

    @property
    def runs_root(self) -> Path:
        return self.platform_root / "runs" / "strategies" / self.strategy_id

    def artifact_uri(self, stage: str, artifact_id: str) -> str:
        if not stage.strip() or not artifact_id.strip() or any(
            separator in artifact_id for separator in ("/", "\\")
        ):
            raise ValueError("stage and path-safe artifact_id are required")
        return f"strategy://{self.strategy_id}/{stage}/{artifact_id}"
