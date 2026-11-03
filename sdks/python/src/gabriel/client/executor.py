"""Bounded keyed executor (spec/behavior.md §6.3).

Global concurrency + per-key concurrency + bounded queue. Jobs can temporarily give up their
permits (``suspend``) while awaiting a reply and re-acquire them ahead of queued work
(``resume``, head-of-line priority), which prevents re-entrancy deadlocks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, Protocol, TypeVar

from ..util.aio import spawn

M = TypeVar("M")


class JobHandle(Protocol):
    def suspend(self) -> None:
        """Releases the key + global permits (e.g. while waiting for a reply)."""

    async def resume(self) -> None:
        """Re-acquires permits with priority over queued jobs."""

    def force_release(self) -> None:
        """Releases permits for good without waiting for the job (handler-timeout grace)."""


@dataclass(slots=True)
class _Job(Generic[M]):
    key: str
    meta: M
    run: Callable[[JobHandle], Awaitable[None]]


@dataclass(slots=True)
class _Resumer:
    key: str
    grant: Callable[[], None]


class _Handle:
    def __init__(self, executor: KeyedExecutor[Any], key: str) -> None:
        self._ex = executor
        self._key = key
        self.held = True
        self.done = False
        self._resuming: asyncio.Future[None] | None = None

    def suspend(self) -> None:
        if not self.held or self.done:
            return
        self.held = False
        self._ex._release(self._key)
        self._ex._pump()

    async def resume(self) -> None:
        if self.held or self.done:
            return
        fut = self._resuming
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self._resuming = fut

            def grant(fut: asyncio.Future[None] = fut) -> None:
                self._resuming = None
                if self.done:
                    # The job finished (or was force-released) while waiting: give the permit back.
                    self._ex._release(self._key)
                    self._ex._pump()
                else:
                    self.held = True
                if not fut.done():
                    fut.set_result(None)

            self._ex._resumers.append(_Resumer(self._key, grant))
            self._ex._pump()
        # The grant may run synchronously inside _pump() and clear self._resuming.
        await asyncio.shield(fut)

    def force_release(self) -> None:
        if self.done:
            return
        self.done = True
        if self.held:
            self.held = False
            self._ex._release(self._key)
        self._ex._finish_job()


class KeyedExecutor(Generic[M]):
    def __init__(self, *, global_concurrency: int, per_key_concurrency: int, max_queue: int) -> None:
        self._global_concurrency = global_concurrency
        self._per_key_concurrency = per_key_concurrency
        self._max_queue = max_queue
        self._queues: dict[str, list[_Job[M]]] = {}
        self._running: dict[str, int] = {}
        self._global_running = 0
        self._queued = 0
        self._resumers: list[_Resumer] = []
        self._active = 0
        self._idle_waiters: list[asyncio.Future[None]] = []
        self._tasks: set[asyncio.Task[Any]] = set()

    @property
    def queue_depth(self) -> int:
        return self._queued

    @property
    def active_jobs(self) -> int:
        return self._active

    def submit(self, key: str, meta: M, run: Callable[[JobHandle], Awaitable[None]]) -> bool:
        """Enqueues a job; returns False when the queue is full (caller handles overflow)."""
        if self._queued >= self._max_queue:
            return False
        self._queues.setdefault(key, []).append(_Job(key, meta, run))
        self._queued += 1
        self._pump()
        return True

    def extract_pending(self, key: str, predicate: Callable[[M], bool]) -> tuple[bool, M | None]:
        """Removes the first queued job for ``key`` whose meta matches; returns ``(found, meta)``."""
        q = self._queues.get(key)
        if not q:
            return False, None
        for i, job in enumerate(q):
            if predicate(job.meta):
                del q[i]
                self._queued -= 1
                if not q:
                    del self._queues[key]
                return True, job.meta
        return False, None

    async def idle(self) -> None:
        """Returns when no job is queued or running."""
        if self._active == 0 and self._queued == 0:
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._idle_waiters.append(fut)
        await fut

    def _can_run(self, key: str) -> bool:
        return self._global_running < self._global_concurrency and self._running.get(key, 0) < self._per_key_concurrency

    def _acquire(self, key: str) -> None:
        self._global_running += 1
        self._running[key] = self._running.get(key, 0) + 1

    def _release(self, key: str) -> None:
        self._global_running -= 1
        n = self._running.get(key, 1) - 1
        if n <= 0:
            self._running.pop(key, None)
        else:
            self._running[key] = n

    def _pump(self) -> None:
        # Resuming jobs first (head-of-line priority).
        i = 0
        while i < len(self._resumers):
            r = self._resumers[i]
            if self._can_run(r.key):
                self._resumers.pop(i)
                self._acquire(r.key)
                r.grant()
            else:
                i += 1
        for key in list(self._queues):
            if self._global_running >= self._global_concurrency:
                break
            q = self._queues[key]
            while q and self._can_run(key) and not any(r.key == key for r in self._resumers):
                job = q.pop(0)
                self._queued -= 1
                self._start(job)
            if not q:
                del self._queues[key]

    def _start(self, job: _Job[M]) -> None:
        self._acquire(job.key)
        self._active += 1
        handle = _Handle(self, job.key)
        spawn(self._run(job, handle), self._tasks)

    async def _run(self, job: _Job[M], handle: _Handle) -> None:
        try:
            await job.run(handle)
        except Exception:
            pass
        finally:
            if not handle.done:
                handle.done = True
                if handle.held:
                    handle.held = False
                    self._release(job.key)
                self._finish_job()

    def _finish_job(self) -> None:
        self._active -= 1
        self._pump()
        if self._active == 0 and self._queued == 0:
            waiters, self._idle_waiters = self._idle_waiters, []
            for w in waiters:
                if not w.done():
                    w.set_result(None)
