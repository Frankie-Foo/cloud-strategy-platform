from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any


def _load(path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location("custom_strategy", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("strategy module is unavailable")
    module = importlib.util.module_from_spec(spec)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        spec.loader.exec_module(module)
    return module


def _payload(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("contract_version") != "strategy-input.v1":
        raise ValueError("invalid input contract")
    return value


def main() -> int:
    if len(sys.argv) != 3:
        return 2
    evaluate = getattr(_load(Path(sys.argv[1])), "evaluate", None)
    if not callable(evaluate):
        return 3
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        result = evaluate(_payload(Path(sys.argv[2])))
    if not isinstance(result, dict):
        return 4
    action, reason = result.get("action"), result.get("reason")
    if action not in {"watch", "enter_long", "exit_long"}:
        return 5
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 512:
        return 6
    print(json.dumps({"action": action, "reason": reason}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
