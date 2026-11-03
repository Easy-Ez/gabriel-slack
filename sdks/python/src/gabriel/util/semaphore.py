"""Send-path concurrency primitives."""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from ..errors import cancelled_error
from .aio import AbortSignal

SendPriority = Literal["interactive", "normal", "stream_update"]

_RANK: dict[str, int] = {"interactive": 0, "normal": 1, "stream_update": 2}


@dataclass(order=True)
class _Waiter:
    rank: int
    seq: int
    future: asyncio.Future[None] = field(compare=False)
    priority: SendPriority = field(compare=False)


class PrioritySemaphore:
    """Global send semaphore with three priorities.

    ``reserved`` slots are only usable by ``interactive`` work, so direct replies still go
    out during an alert storm. :meth:`acquire` returns an idempotent release callable.
    """

    def __init__(self, total: int, reserved: int = 1) -> None:
        if total < 1:
            raise ValueError("semaphore total must be >= 1")
        self._total = total
        self._reserved = reserved
        self._in_use = 0
        self._seq = itertools.count()
        self._waiters: list[_Waiter] = []

    def _allowed(self, priority: SendPriority) -> bool:
        limit = self._total if priority == "interactive" else max(1, self._total - self._reserved)
        return self._in_use < limit

    async def acquire(self, priority: SendPriority, signal: AbortSignal | None = None) -> Callable[[], None]:
        if signal is not None:
            signal.throw_if_aborted()
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        waiter = _Waiter(_RANK[priority], next(self._seq), fut, priority)
        self._waiters.append(waiter)
        self._waiters.sort()
        self._pump()

        def on_abort() -> None:
            if not fut.done():
                fut.set_exception(cancelled_error(signal.reason if signal else None))

        remove = signal.add_listener(on_abort) if signal is not None else None
        try:
            await fut
        except BaseException:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            elif fut.done() and not fut.cancelled() and fut.exception() is None:
                # Granted concurrently with the cancellation: give the slot back.
                self._release()
            raise
        finally:
            if remove is not None:
                remove()

        released = False

        def release() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._release()

        return release

    def _release(self) -> None:
        self._in_use -= 1
        self._pump()

    def _pump(self) -> None:
        i = 0
        while i < len(self._waiters):
            w = self._waiters[i]
            if w.future.done():
                self._waiters.pop(i)
                continue
            if self._allowed(w.priority):
                self._waiters.pop(i)
                self._in_use += 1
                w.future.set_result(None)
            else:
                i += 1


class TokenBucket:
    """Token bucket shared by all concurrent streams (round-robin fairness via FIFO waiters)."""

    def __init__(self, rate_per_sec: float, burst: float, now: Callable[[], float] = time.monotonic) -> None:
        self._rate = rate_per_sec
        self._burst = burst
        self._now = now
        self._tokens = float(burst)
        self._last = now()
        self._waiters: list[asyncio.Future[None]] = []
        self._timer: asyncio.TimerHandle | None = None

    async def take(self, signal: AbortSignal | None = None) -> None:
        if signal is not None:
            signal.throw_if_aborted()
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append(fut)
        self._pump()

        def on_abort() -> None:
            if not fut.done():
                fut.set_exception(cancelled_error(signal.reason if signal else None))

        remove = signal.add_listener(on_abort) if signal is not None else None
        try:
            await fut
        finally:
            if fut in self._waiters:
                self._waiters.remove(fut)
            if remove is not None:
                remove()

    def _refill(self) -> None:
        t = self._now()
        self._tokens = min(self._burst, self._tokens + (t - self._last) * self._rate)
        self._last = t

    def _pump(self) -> None:
        self._refill()
        self._waiters = [w for w in self._waiters if not w.done()]
        while self._waiters and self._tokens >= 1:
            self._tokens -= 1
            self._waiters.pop(0).set_result(None)
        if self._waiters and self._timer is None:
            wait = max(0.001, (1 - self._tokens) / self._rate)

            def fire() -> None:
                self._timer = None
                self._pump()

            self._timer = asyncio.get_running_loop().call_later(wait, fire)
