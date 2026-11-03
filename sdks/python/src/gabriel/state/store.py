"""Pluggable persistence for recovery watermarks and the watched-thread list."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from ..util.validate import compare_ts


@runtime_checkable
class StateStore(Protocol):
    """Persistence contract. Values are opaque strings; ``ttl`` is in seconds."""

    #: True when state survives process restarts (enables restart backfill).
    persistent: bool

    async def get(self, key: str) -> str | None: ...
    async def set(self, key: str, value: str, *, ttl: float | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def set_if_absent(self, key: str, value: str, *, ttl: float | None = None) -> bool:
        """Sets the key only when absent. Returns True when this call created it (lock primitive)."""
        ...

    async def max_merge_ts(self, key: str, ts: str) -> str:
        """Monotonic max-merge for Slack timestamps; returns the value now stored."""
        ...

    async def keys(self, prefix: str) -> list[str]: ...
    async def close(self) -> None:
        """Flushes pending writes and releases resources."""
        ...


@dataclass(slots=True)
class _Entry:
    value: str
    expires_at: float | None = None


class MemoryStateStore:
    """In-memory store (default). State is lost on restart."""

    persistent: bool = False

    def __init__(self, now: Callable[[], float] = time.time) -> None:
        self._now = now
        self._data: dict[str, _Entry] = {}

    def _live(self, key: str) -> _Entry | None:
        e = self._data.get(key)
        if e is not None and e.expires_at is not None and e.expires_at <= self._now():
            del self._data[key]
            return None
        return e

    async def get(self, key: str) -> str | None:
        e = self._live(key)
        return e.value if e else None

    async def set(self, key: str, value: str, *, ttl: float | None = None) -> None:
        self._data[key] = _Entry(value, None if ttl is None else self._now() + ttl)
        self._changed()

    async def delete(self, key: str) -> None:
        self._data.pop(key, None)
        self._changed()

    async def set_if_absent(self, key: str, value: str, *, ttl: float | None = None) -> bool:
        if self._live(key):
            return False
        await self.set(key, value, ttl=ttl)
        return True

    async def max_merge_ts(self, key: str, ts: str) -> str:
        e = self._live(key)
        if e is not None and compare_ts(e.value, ts) >= 0:
            return e.value
        await self.set(key, ts)
        return ts

    async def keys(self, prefix: str) -> list[str]:
        return [k for k in list(self._data) if k.startswith(prefix) and self._live(k) is not None]

    async def close(self) -> None:
        pass

    def _changed(self) -> None:
        """Hook for persistent subclasses."""
