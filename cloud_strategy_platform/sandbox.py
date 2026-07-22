"""Fail-closed container boundary for owner-approved custom Python strategies."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cloud_strategy_platform.contracts import FeatureVector, SignalAction, StrategyDefinition


class PythonSandboxError(RuntimeError):
    pass


class PythonStrategyRunner(Protocol):
    def evaluate(
        self, definition: StrategyDefinition, vector: FeatureVector
    ) -> tuple[SignalAction, str]: ...


@dataclass(frozen=True)
class DockerSandboxPolicy:
    image: str
    timeout_seconds: float = 2.0
    memory: str = "128m"
    cpus: str = "0.5"
    pids_limit: int = 64

    def __post_init__(self) -> None:
        if "@sha256:" not in self.image:
            raise ValueError("sandbox image must be pinned by SHA-256 digest")
        if self.timeout_seconds <= 0 or self.pids_limit <= 0:
            raise ValueError("sandbox resource limits must be positive")

    def command(self, *, source_path: Path, input_path: Path) -> tuple[str, ...]:
        return (
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            f"--pids-limit={self.pids_limit}",
            f"--memory={self.memory}",
            f"--cpus={self.cpus}",
            "--user=65532:65532",
            "--tmpfs=/tmp:rw,noexec,nosuid,size=16m",
            f"--mount=type=bind,src={source_path.resolve()},dst=/strategy/strategy.py,readonly",
            f"--mount=type=bind,src={input_path.resolve()},dst=/strategy/input.json,readonly",
            self.image,
            "/opt/strategy-worker/run.py",
            "/strategy/strategy.py",
            "/strategy/input.json",
        )


class DockerPythonStrategyRunner:
    def __init__(self, policy: DockerSandboxPolicy, *, source_root: Path):
        self.policy = policy
        self.source_root = source_root.resolve()

    def evaluate(
        self, definition: StrategyDefinition, vector: FeatureVector
    ) -> tuple[SignalAction, str]:
        entrypoint = definition.python_entrypoint
        if entrypoint is None or Path(entrypoint).name != entrypoint:
            raise PythonSandboxError("Python entrypoint must be one approved filename")
        source_path = (self.source_root / entrypoint).resolve()
        if source_path.parent != self.source_root or not source_path.is_file():
            raise PythonSandboxError("Python strategy source is unavailable")
        payload = {
            "contract_version": "strategy-input.v1",
            "strategy_id": definition.strategy_id,
            "strategy_version": definition.version,
            "symbol": vector.symbol,
            "asof_utc": vector.asof_utc.isoformat(),
            "parameters": definition.parameters,
            "features": vector.values,
        }
        try:
            with tempfile.TemporaryDirectory(prefix="strategy-input-") as temporary:
                input_path = Path(temporary) / "input.json"
                input_path.write_text(json.dumps(payload), encoding="utf-8")
                completed = subprocess.run(
                    self.policy.command(source_path=source_path, input_path=input_path),
                    capture_output=True,
                    text=True,
                    timeout=self.policy.timeout_seconds,
                    check=False,
                    env={"PATH": os.defpath},
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PythonSandboxError("Python strategy sandbox was unavailable") from exc
        if completed.returncode != 0:
            raise PythonSandboxError("Python strategy sandbox failed")
        try:
            result = json.loads(completed.stdout)
            if not isinstance(result, dict):
                raise ValueError
            action = SignalAction(str(result["action"]))
            reason = str(result["reason"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PythonSandboxError("Python strategy returned an invalid contract") from exc
        if not reason.strip() or len(reason) > 512:
            raise PythonSandboxError("Python strategy reason is invalid")
        return action, reason
