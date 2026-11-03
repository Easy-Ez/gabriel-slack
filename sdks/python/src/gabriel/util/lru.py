"""Minimal LRU map with optional per-entry TTL (seconds)."""

from __future__ import annotations

import math
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class LruCache(Generic[K, V]):
    def __init__(
        self,
        capacity: int,
        default_ttl: float = math.inf,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._capacity = capacity
        self._default_ttl = default_ttl
        self._now = now
        self._map: OrderedDict[K, tuple[V, float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._map)

    @property
    def size(self) -> int:
        return len(self._map)

    def get(self, key: K) -> V | None:
        entry = self._map.get(key)
        if entry is None:
            return None
        if entry[1] <= self._now():
            del self._map[key]
            return None
        self._map.move_to_end(key)
        return entry[0]

    def has(self, key: K) -> bool:
        return self.get(key) is not None

    __contains__ = has

    def set(self, key: K, value: V, ttl: float | None = None) -> None:
        self._map.pop(key, None)
        self._map[key] = (value, self._now() + (self._default_ttl if ttl is None else ttl))
        while len(self._map) > self._capacity:
            self._map.popitem(last=False)

    def delete(self, key: K) -> bool:
        return self._map.pop(key, None) is not None

    def clear(self) -> None:
        self._map.clear()

    def keys(self) -> list[K]:
        return list(self._map.keys())

    def seen(self, key: K, value: V) -> bool:
        """Inserts the key and reports whether it was already present (dedupe helper)."""
        if self.has(key):
            return True
        self.set(key, value)
        return False
