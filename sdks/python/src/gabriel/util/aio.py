"""asyncio helpers shared by every layer.

:class:`AbortSignal` / :class:`AbortController` mirror the web-platform primitives the
reference SDK uses for cancellation. Public APIs accept an optional ``signal=`` and also
honor ordinary ``asyncio`` task cancellation.
"""

from __future__ import annotations

import asyncio
import random as _random
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Generic, TypeVar

from ..errors import cancelled_error

T = TypeVar("T")


class AbortSignal:
    """A one-shot cancellation flag with listeners."""

    __slots__ = ("_disposers", "_listeners", "_reason", "aborted")

    def __init__(self) -> None:
        self.aborted = False
        self._reason: object = None
        self._listeners: list[Callable[[], None]] = []
        #: Detaches a derived signal (``timeout`` / ``any``) from its sources.
        self._disposers: list[Callable[[], None]] = []

    def dispose(self) -> None:
        """Releases a derived signal: cancels its timer and removes its listeners on input signals.

        Node's derived signals are weakly referenced; in Python call this once a signal made by
        :meth:`timeout` or :meth:`any` is no longer needed, so long-lived inputs do not accumulate
        listeners. Safe to call more than once; a no-op for plain signals.
        """
        disposers, self._disposers = self._disposers, []
        for d in disposers:
            d()

    @property
    def reason(self) -> object:
        return self._reason

    def add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Calls ``listener`` once on abort (immediately if already aborted). Returns a remover."""
        if self.aborted:
            listener()
            return lambda: None
        self._listeners.append(listener)

        def remove() -> None:
            try:
                self._listeners.remove(listener)
            except ValueError:
                pass

        return remove

    def throw_if_aborted(self) -> None:
        if self.aborted:
            raise cancelled_error(self._reason)

    async def wait(self) -> None:
        """Returns once the signal is aborted."""
        if self.aborted:
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        def done() -> None:
            if not fut.done():
                fut.set_result(None)

        remove = self.add_listener(done)
        try:
            await fut
        finally:
            remove()

    def _abort(self, reason: object) -> None:
        if self.aborted:
            return
        self.aborted = True
        self._reason = reason
        listeners, self._listeners = self._listeners, []
        for listener in listeners:
            listener()

    @staticmethod
    def aborted_with(reason: object = None) -> AbortSignal:
        s = AbortSignal()
        s._abort(reason if reason is not None else cancelled_error())
        return s

    @staticmethod
    def timeout(seconds: float) -> AbortSignal:
        """A signal that aborts with NETWORK_TIMEOUT after ``seconds`` (like ``AbortSignal.timeout``)."""
        from ..errors import GabrielError

        s = AbortSignal()
        handle = asyncio.get_running_loop().call_later(
            max(0.0, seconds), lambda: s._abort(GabrielError("NETWORK_TIMEOUT", "signal timed out"))
        )
        s._disposers.append(handle.cancel)
        return s

    @staticmethod
    def any(*signals: AbortSignal | None) -> AbortSignal:
        """Combines optional signals into one that aborts when any input aborts."""
        real = [s for s in signals if s is not None]
        if len(real) == 1:
            return real[0]
        combined = AbortSignal()
        for s in real:
            if s.aborted:
                combined._abort(s.reason)
                return combined
        for s in real:
            combined._disposers.append(s.add_listener(lambda s=s: combined._abort(s.reason)))  # type: ignore[misc]
        return combined


class AbortController:
    __slots__ = ("signal",)

    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: object = None) -> None:
        self.signal._abort(reason if reason is not None else cancelled_error())


async def sleep(seconds: float, signal: AbortSignal | None = None) -> None:
    """Sleeps ``seconds``, raising CANCELLED as soon as ``signal`` aborts."""
    if signal is None:
        await asyncio.sleep(max(0.0, seconds))
        return
    signal.throw_if_aborted()
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[None] = loop.create_future()

    def wake() -> None:
        if not fut.done():
            fut.set_result(None)

    handle = loop.call_later(max(0.0, seconds), wake)

    def on_abort() -> None:
        if not fut.done():
            fut.set_exception(cancelled_error(signal.reason))

    remove = signal.add_listener(on_abort)
    try:
        await fut
    finally:
        handle.cancel()
        remove()


async def race_signal(awaitable: Awaitable[T], signal: AbortSignal | None) -> T:
    """Awaits ``awaitable`` but raises CANCELLED (and cancels it) when ``signal`` aborts."""
    if signal is None:
        return await awaitable
    signal.throw_if_aborted()
    task = asyncio.ensure_future(awaitable)

    def on_abort() -> None:
        task.cancel()

    remove = signal.add_listener(on_abort)
    try:
        return await task
    except asyncio.CancelledError:
        if signal.aborted and not _current_task_cancelling():
            raise cancelled_error(signal.reason) from None
        raise
    finally:
        remove()


def _current_task_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def with_timeout(awaitable: Awaitable[T], seconds: float, on_timeout: Callable[[], BaseException]) -> T:
    """Resolves ``awaitable`` or raises ``on_timeout()`` after ``seconds``."""
    try:
        return await asyncio.wait_for(awaitable, timeout=seconds)
    except TimeoutError:
        raise on_timeout() from None


class Deferred(Generic[T]):
    """A future that can be resolved from outside (``promise`` / ``resolve`` / ``reject``)."""

    __slots__ = ("future",)

    def __init__(self) -> None:
        self.future: asyncio.Future[T] = asyncio.get_running_loop().create_future()

    def resolve(self, value: T) -> None:
        if not self.future.done():
            self.future.set_result(value)

    def reject(self, err: BaseException) -> None:
        if not self.future.done():
            self.future.set_exception(err)
            # Mark retrieved so an unawaited rejection does not log "exception never retrieved".
            self.future.exception()

    def done(self) -> bool:
        return self.future.done()

    def __await__(self) -> Any:
        return self.future.__await__()


class Singleflight(Generic[T]):
    """Collapses concurrent calls with the same key into one in-flight task."""

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Future[T]] = {}

    async def do(self, key: str, fn: Callable[[], Awaitable[T]]) -> T:
        existing = self._inflight.get(key)
        if existing is None:
            existing = asyncio.ensure_future(fn())
            self._inflight[key] = existing
            existing.add_done_callback(lambda _f: self._inflight.pop(key, None))
        # shield: one caller being cancelled must not cancel the shared work.
        return await asyncio.shield(existing)


def full_jitter_delay(attempt: int, base: float, cap: float, random: Callable[[], float] = _random.random) -> float:
    """Full-jitter exponential backoff: ``random(0, min(cap, base * 2**attempt))`` seconds."""
    ceiling: float = min(cap, base * 2.0 ** max(0, attempt))
    return random() * ceiling


def chunk(items: Sequence[T], size: int) -> list[list[T]]:
    """Splits a sequence into fixed-size chunks."""
    return [list(items[i : i + size]) for i in range(0, len(items), size)]


def spawn(coro: Awaitable[Any], tasks: set[asyncio.Task[Any]] | None = None) -> asyncio.Task[Any]:
    """Starts a background task, keeping a strong reference until it finishes."""
    task = asyncio.ensure_future(coro)
    if tasks is not None:
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    else:
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
    return task


_BACKGROUND: set[asyncio.Task[Any]] = set()
