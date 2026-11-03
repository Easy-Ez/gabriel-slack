"""DataLoader-style batched entity lookups."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Generic, TypeVar

from ..errors import GabrielError
from ..util.aio import Deferred, chunk, spawn
from ..util.lru import LruCache

V = TypeVar("V")

_MISSING: Any = object()


class BatchLoader(Generic[V]):
    """Coalesces lookups inside a short window into batched requests.

    Shares in-flight requests (singleflight), caches positives, and caches "not found" results
    for 10 minutes. Transient errors are never cached.

    Args:
        batch_fn: Loads a batch; ids missing from the returned mapping are treated as "not found".
    """

    def __init__(
        self,
        batch_fn: Callable[[list[str]], Awaitable[Mapping[str, V]]],
        *,
        window: float = 0.010,
        max_batch: int = 100,
        concurrency: int = 2,
        ttl: float = 60 * 60.0,
        negative_ttl: float = 10 * 60.0,
        capacity: int = 20_000,
    ) -> None:
        self._batch_fn = batch_fn
        self._window = window
        self._max_batch = max_batch
        self._concurrency = concurrency
        self._ttl = ttl
        self._negative_ttl = negative_ttl
        self._cache: LruCache[str, Any] = LruCache(capacity)
        self._inflight: dict[str, Deferred[V | None]] = {}
        self._queue: list[str] = []
        self._timer: asyncio.TimerHandle | None = None
        self._running = 0
        self._pending_batches: list[list[str]] = []
        self._tasks: set[asyncio.Task[Any]] = set()

    def _cached(self, id: str) -> Any:
        # LruCache.get returns None for "absent"; store a sentinel for "known missing".
        value = self._cache.get(id)
        return _MISSING if value is None else value

    def peek(self, id: str) -> V | None:
        """Synchronous cache read: the value, or None when unknown or known missing."""
        value = self._cached(id)
        return None if value is _MISSING or value is _NOT_FOUND else value

    def is_known(self, id: str) -> bool:
        """True when the cache holds a value or a "not found" marker for ``id``."""
        return self._cached(id) is not _MISSING

    def prime(self, id: str, value: V) -> None:
        self._cache.set(id, value, self._ttl)

    def invalidate(self, id: str) -> None:
        self._cache.delete(id)

    def load(self, id: str, *, fresh: bool = False) -> asyncio.Future[V | None]:
        loop = asyncio.get_running_loop()
        if not fresh:
            cached = self._cached(id)
            if cached is not _MISSING:
                fut: asyncio.Future[V | None] = loop.create_future()
                fut.set_result(None if cached is _NOT_FOUND else cached)
                return fut
        existing = self._inflight.get(id)
        if existing is not None:
            return existing.future
        d: Deferred[V | None] = Deferred()
        self._inflight[id] = d
        self._queue.append(id)
        if len(self._queue) >= self._max_batch:
            self._flush()
        elif self._timer is None:
            self._timer = loop.call_later(self._window, self._flush)
        return d.future

    async def load_many(self, ids: list[str]) -> list[V | None]:
        return list(await asyncio.gather(*(self.load(id) for id in ids)))

    def _flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        ids, self._queue = self._queue, []
        self._pending_batches.extend(chunk(ids, self._max_batch))
        self._pump()

    def _pump(self) -> None:
        while self._running < self._concurrency and self._pending_batches:
            batch = self._pending_batches.pop(0)
            self._running += 1
            spawn(self._run_and_continue(batch), self._tasks)

    async def _run_and_continue(self, batch: list[str]) -> None:
        try:
            await self._run_batch(batch)
        finally:
            self._running -= 1
            self._pump()

    async def _run_batch(self, ids: list[str]) -> None:
        try:
            found = await self._batch_fn(ids)
        except Exception as err:
            error = (
                err
                if isinstance(err, GabrielError)
                else GabrielError("SERVER_ERROR", "entity lookup failed", cause=err)
            )
            for id in ids:
                d = self._inflight.pop(id, None)
                if d is not None:
                    d.reject(error)
            return
        for id in ids:
            value = found.get(id)
            if value is None:
                self._cache.set(id, _NOT_FOUND, self._negative_ttl)
            else:
                self._cache.set(id, value, self._ttl)
            d = self._inflight.pop(id, None)
            if d is not None:
                d.resolve(value)


_NOT_FOUND: Any = object()
