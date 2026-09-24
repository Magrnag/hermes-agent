"""Small cross-platform advisory locks for operations mutations."""

from __future__ import annotations

import contextlib
import importlib
import os
import time
from pathlib import Path
from typing import Any, Iterator

fcntl: Any = importlib.import_module("fcntl") if os.name != "nt" else None
msvcrt: Any = importlib.import_module("msvcrt") if os.name == "nt" else None


@contextlib.contextmanager
def file_lock(path: str | Path, *, timeout: float | None = 10.0) -> Iterator[None]:
    """Hold an advisory process lock; lock files are never interpreted as user paths."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                elif msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError):
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"timed out acquiring operations lock: {lock_path.name}"
                    )
                time.sleep(0.02)
        try:
            yield
        finally:
            with contextlib.suppress(OSError):
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class OperationLocks:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def ledger(self, *, timeout: float | None = 10.0):
        return file_lock(self.root / "ledger.lock", timeout=timeout)

    def workspace(self, name: str, *, timeout: float | None = 10.0):
        # The caller has already resolved the name through an explicit registry.
        return file_lock(self.root / f"workspace-{name}.lock", timeout=timeout)
