"""Single-host persistent state store."""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import Callable
from pathlib import Path

from ..errors import GabrielError
from .store import MemoryStateStore, _Entry


class FileStateStore(MemoryStateStore):
    """An in-memory map snapshotted to ``{dir}/state.json``.

    Writes use an atomic temp-file rename with 0600 permissions. Enables restart backfill from
    persisted watermarks. Not safe for multiple processes sharing one directory. The file format
    (``expiresAt`` in epoch milliseconds) is shared with the Node SDK.

    Args:
        dir: Directory holding ``state.json``. Created with 0700 permissions when missing.
        flush_interval: Write debounce in seconds (default 1 s).
    """

    persistent: bool = True

    def __init__(
        self, dir: str | os.PathLike[str], *, flush_interval: float = 1.0, now: Callable[[], float] = time.time
    ):
        super().__init__(now)
        raw = os.fspath(dir)
        if not isinstance(raw, str) or not raw or "\0" in raw:
            raise GabrielError("INVALID_ARGUMENT", "FileStateStore.dir must be a non-empty path")
        path = Path(raw).resolve()
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.is_dir():
            raise GabrielError("INVALID_ARGUMENT", "FileStateStore.dir is not a directory")
        self._file = path / "state.json"
        self._flush_interval = flush_interval
        self._timer: asyncio.TimerHandle | None = None
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            parsed = json.loads(self._file.read_text("utf-8"))
            for k, e in parsed.items():
                if isinstance(e, dict) and isinstance(e.get("value"), str):
                    exp = e.get("expiresAt")
                    self._data[k] = _Entry(e["value"], None if exp is None else exp / 1000)
        except Exception as err:
            raise GabrielError("STATE_STORE_UNAVAILABLE", f"cannot read {self._file}", cause=err) from err

    def _changed(self) -> None:
        self._dirty = True
        if self._timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush_sync()
            return

        def fire() -> None:
            self._timer = None
            self.flush_sync()

        self._timer = loop.call_later(self._flush_interval, fire)

    def flush_sync(self) -> None:
        """Writes the snapshot now (atomic rename)."""
        if not self._dirty:
            return
        now = self._now()
        snapshot: dict[str, dict[str, object]] = {}
        for k, e in self._data.items():
            if e.expires_at is None:
                snapshot[k] = {"value": e.value}
            elif e.expires_at > now:
                snapshot[k] = {"value": e.value, "expiresAt": int(e.expires_at * 1000)}
        tmp = self._file.with_name(f"{self._file.name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self._file)
        self._dirty = False

    async def close(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        self.flush_sync()
