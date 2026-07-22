from __future__ import annotations

import json
import os
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import IO


class LockUnavailableError(RuntimeError):
    pass


class ProcessLock:
    _guard = threading.Lock()
    _held: set[Path] = set()

    def __init__(self, path: Path):
        self.path = path.resolve()
        self._handle: IO[str] | None = None

    def __enter__(self) -> ProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._guard:
            if self.path in self._held:
                raise LockUnavailableError("process lock is already held")
            self._held.add(self.path)
        handle: IO[str] | None = None
        try:
            handle = self.path.open("a+", encoding="utf-8")
            if self.path.stat().st_size == 0:
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            self._lock(handle)
            handle.seek(0)
            handle.truncate()
            handle.write(
                json.dumps({"pid": os.getpid(), "started_at_utc": datetime.now(UTC).isoformat()})
            )
            handle.flush()
            os.fsync(handle.fileno())
            self._handle = handle
            return self
        except Exception:
            if handle is not None:
                handle.close()
            with self._guard:
                self._held.discard(self.path)
            raise

    @staticmethod
    def _lock(handle: IO[str]) -> None:
        if sys.platform == "win32":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise LockUnavailableError("process lock is held") from exc
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise LockUnavailableError("process lock is held") from exc

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        handle = self._handle
        try:
            if handle is not None:
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
        finally:
            self._handle = None
            with self._guard:
                self._held.discard(self.path)
