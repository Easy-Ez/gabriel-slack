"""Streaming replies via placeholder + ``chat.update`` (spec/behavior.md §9)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import Any, Protocol

from ..errors import GabrielError
from ..markdown.chunker import chunk_markdown, heal_open_fence, measure_markdown
from ..util.aio import AbortController, AbortSignal, sleep
from ..util.logger import Logger
from ..util.semaphore import TokenBucket
from .origin import SelfRegistry


@dataclass(slots=True)
class StreamResult:
    #: ts of the first (placeholder) message.
    ts: str
    #: ts of every continuation message posted after the budget was exceeded.
    continuation_ts: list[str] = field(default_factory=list)


class StreamController(Protocol):
    #: ts of the first (placeholder) message.
    @property
    def ts(self) -> str: ...

    #: Aborted when the streamed message is deleted in Slack (or the linked ctx.signal fires).
    @property
    def signal(self) -> AbortSignal: ...

    def append(self, chunk: str) -> None:
        """Appends text. Synchronous O(1): only the in-memory buffer changes."""

    async def finish(self) -> StreamResult:
        """Flushes the final content and closes the stream."""
        ...

    async def abort(self, reason: str = "aborted") -> None:
        """Stops refreshing and closes the stream. Already-posted messages stay."""

    async def __aenter__(self) -> StreamController: ...

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None: ...


class StreamTransport(Protocol):
    async def post(self, markdown: str, thread_ts: str | None) -> str: ...
    async def update(self, ts: str, markdown: str) -> None: ...


@dataclass(kw_only=True, slots=True)
class StreamOptions:
    channel: str
    budget: int
    min_interval: float
    thread_ts: str | None = None
    linked_signal: AbortSignal | None = None


_ECHO_GRACE = 15.0


class StreamControllerImpl:
    """Single in-flight, latest-wins streaming controller.

    ``append()`` bumps a dirty generation; at most one ``chat.update`` per stream is in flight;
    when it returns and newer content exists, the newest snapshot is sent. The final message
    content equals the buffer at ``finish()``. Use as ``async with`` to finish on success and
    abort on error.
    """

    def __init__(
        self,
        first_ts: str,
        opts: StreamOptions,
        transport: StreamTransport,
        bucket: TokenBucket,
        registry: SelfRegistry,
        logger: Logger,
        on_closed: Callable[[StreamControllerImpl], None],
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ts = first_ts
        self._opts = opts
        self._transport = transport
        self._bucket = bucket
        self._registry = registry
        self._logger = logger
        self._on_closed = on_closed
        self._now = now
        self._controller = AbortController()
        self._current = ""
        self._current_ts = first_ts
        self._continuation_ts: list[str] = []
        self._dirty_gen = 0
        self._sent_gen = 0
        self._inflight: asyncio.Task[None] | None = None
        self._closed = False
        self._last_flush_at = float("-inf")
        self._budget = opts.budget
        self._all_ts = [first_ts]
        registry.begin_stream(first_ts)
        if opts.linked_signal is not None and not opts.linked_signal.aborted:
            linked = opts.linked_signal
            linked.add_listener(lambda: self._cancel(linked.reason or "cancelled"))

    @property
    def ts(self) -> str:
        return self._ts

    @property
    def signal(self) -> AbortSignal:
        return self._controller.signal

    @property
    def message_ts(self) -> list[str]:
        return list(self._all_ts)

    def append(self, chunk: str) -> None:
        if self._closed:
            raise GabrielError("STREAM_CLOSED", "stream already finished or aborted")
        if not isinstance(chunk, str) or len(chunk) == 0:
            return
        self._current += chunk
        self._dirty_gen += 1
        self._schedule()

    async def finish(self) -> StreamResult:
        if self._closed and self._controller.signal.aborted:
            raise GabrielError("STREAM_CLOSED", "stream was aborted")
        self._closed = True
        await self._drain(True)
        self._release()
        return StreamResult(self._ts, list(self._continuation_ts))

    async def abort(self, reason: str = "aborted") -> None:
        if self._closed and self._controller.signal.aborted:
            return
        self._closed = True
        self._controller.abort(reason)
        if self._inflight is not None:
            await _settle(self._inflight)
        self._release()

    async def __aenter__(self) -> StreamControllerImpl:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._closed:
            return
        if exc is None:
            await self.finish()
        else:
            await self.abort(type(exc).__name__)

    async def interrupt(self, notice: str) -> None:
        """Graceful shutdown: append a notice and flush what we have."""
        if self._closed:
            return
        self._current += notice
        self._dirty_gen += 1
        try:
            await self.finish()
        except Exception as err:
            self._logger.warn({"err": str(err)}, "stream interrupt flush failed")

    def notify_deleted(self, ts: str) -> None:
        """Called by the client when any of this stream's messages is deleted."""
        if ts in self._all_ts:
            self._cancel("message_deleted")

    def _cancel(self, reason: object) -> None:
        if self._controller.signal.aborted:
            return
        self._closed = True
        self._controller.abort(reason)
        self._release()

    def _release(self) -> None:
        ts = list(self._all_ts)

        def end() -> None:
            for t in ts:
                self._registry.end_stream(t)

        # Keep echo suppression alive briefly for late edit echoes.
        try:
            asyncio.get_running_loop().call_later(_ECHO_GRACE, end)
        except RuntimeError:
            end()
        self._on_closed(self)

    def _start_flush(self, final: bool, on_done: Callable[[], None] | None = None) -> None:
        task = asyncio.ensure_future(self._flush_once(final))
        self._inflight = task

        def done(t: asyncio.Task[None]) -> None:
            if not t.cancelled():
                t.exception()  # retrieved by _drain / logged in _flush_once
            if self._inflight is t:
                self._inflight = None
            if on_done is not None:
                on_done()

        task.add_done_callback(done)

    def _schedule(self) -> None:
        if self._inflight is not None or self._controller.signal.aborted:
            return
        if self._dirty_gen <= self._sent_gen:
            return

        def again() -> None:
            if not self._closed:
                self._schedule()

        self._start_flush(False, again)

    async def _drain(self, final: bool) -> None:
        for _ in range(100):
            if self._inflight is not None:
                await _settle(self._inflight)
            if self._controller.signal.aborted:
                return
            if self._dirty_gen <= self._sent_gen:
                return
            self._start_flush(final)

    async def _flush_once(self, final: bool) -> None:
        signal = self._controller.signal
        try:
            if not final:
                wait = self._last_flush_at + self._opts.min_interval - self._now()
                if wait > 0:
                    await sleep(wait, signal)
            await self._bucket.take(signal)
            gen = self._dirty_gen
            if measure_markdown(self._current).effective > self._budget:
                await self._rollover()
            body = self._current if len(self._current) > 0 else "…"
            await self._transport.update(self._current_ts, body if final else heal_open_fence(body))
            self._last_flush_at = self._now()
            if gen > self._sent_gen:
                self._sent_gen = gen
        except Exception as err:
            if signal.aborted:
                return
            code = getattr(err, "code", None)
            raw = getattr(err, "raw_slack_error", None)
            if code in ("MSG_TOO_LONG", "MSG_BLOCKS_TOO_LONG"):
                self._budget = int(self._budget * 0.8)
                return
            if raw == "message_not_found":
                self._cancel("message_deleted")
                return
            self._logger.warn(
                {"err": str(err), "code": code}, "stream refresh failed; will retry with the latest content"
            )
            try:
                await sleep(1.0, signal)
            except GabrielError:
                pass
            if final:
                raise

    async def _rollover(self) -> None:
        """Finalizes the current message with the head of the buffer and continues in a new message."""
        # `append()` may run while we await; only the prefix we split is replaced.
        base = len(self._current)
        pieces = chunk_markdown(self._current, self._budget, int(self._budget * 0.7))
        while len(pieces) > 1:
            head = pieces.pop(0)
            await self._transport.update(self._current_ts, head)
            nxt = pieces[0]
            new_ts = await self._transport.post(heal_open_fence(nxt if len(nxt) > 0 else "…"), self._opts.thread_ts)
            self._registry.begin_stream(new_ts)
            self._all_ts.append(new_ts)
            self._continuation_ts.append(new_ts)
            self._current_ts = new_ts
            # Trim after every successful hand-over so a later failure never re-sends finalized text.
            appended = self._current[base:]
            rest = "\n\n".join(pieces)
            self._current = rest + appended
            base = len(rest)


async def _settle(aw: Awaitable[Any]) -> None:
    try:
        await asyncio.shield(aw)
    except Exception:
        pass
