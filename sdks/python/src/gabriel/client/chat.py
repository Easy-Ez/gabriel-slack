"""Sending: ordered, chunked, idempotent posts, edits, deletes and streams (spec/behavior.md §9)."""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from ..core.types import RequestOpts
from ..errors import GabrielError
from ..events.normalize import content_hash
from ..markdown.chunker import DEFAULT_CHUNK_BUDGET, DEFAULT_CHUNK_MIN, chunk_markdown
from ..util.aio import AbortSignal, sleep
from ..util.logger import Logger
from ..util.lru import LruCache
from ..util.semaphore import PrioritySemaphore, SendPriority, TokenBucket
from ..util.validate import assert_channel_id, assert_ts, is_ts
from .origin import SelfRegistry
from .stream import StreamController, StreamControllerImpl, StreamOptions

if TYPE_CHECKING:
    from ..core.core import GabrielCore

T = TypeVar("T")


@dataclass(slots=True)
class SendResult:
    channel: str
    #: ts of the first message.
    ts: str
    #: ts of every message posted (several when the Markdown was split).
    chunk_ts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class UpdateResult:
    channel: str
    ts: str


@dataclass(kw_only=True, slots=True)
class ChatSchedulerOptions:
    #: Cross-channel send concurrency; one slot is reserved for ``interactive`` (default 4).
    global_send_concurrency: int = 4
    #: Hard deadline per send task in seconds (default 30).
    item_deadline: float = 30.0
    #: Workspace-wide stream refresh rate shared by all streams (default 2/s, burst 4).
    stream_updates_per_second: float = 2.0
    #: Minimum interval between refreshes of one stream in seconds (default 0.5).
    stream_min_interval: float = 0.5
    #: Notice appended to unfinished streams on ``client.stop()``.
    interrupted_notice: str = "\n\n_(interrupted)_"


class ChatHooks(Protocol):
    """Hooks implemented by the client (loop guard, unread-cursor guard, recovery bookkeeping)."""

    def before_auto_write(self, channel: str, thread_ts: str | None) -> None: ...
    async def with_unread_guard(self, channel: str, enabled: bool, fn: Callable[[], Awaitable[T]]) -> T: ...
    def note_thread_activity(self, channel: str, thread_ts: str) -> None: ...


@dataclass(kw_only=True, slots=True)
class _Msg:
    channel: str
    markdown: str | None = None
    blocks: list[Any] | None = None
    thread_ts: str | None = None
    broadcast: bool = False


_CHAT_WINDOW = 2.0
_NOT_FOUND_RETRY = 0.175
_NOT_FOUND_RETRIES = 10


def _ts_to_seconds(ts: str) -> float:
    sec, _, frac = ts.partition(".")
    return int(sec) + int((frac or "0").ljust(6, "0")[:3]) / 1000


class ChatApi:
    def __init__(
        self,
        core: GabrielCore,
        registry: SelfRegistry,
        hooks: ChatHooks,
        logger: Logger,
        opts: ChatSchedulerOptions | None = None,
    ) -> None:
        opts = opts or ChatSchedulerOptions()
        self._core = core
        self._registry = registry
        self._hooks = hooks
        self._logger = logger
        self._semaphore = PrioritySemaphore(opts.global_send_concurrency, 1)
        rate = opts.stream_updates_per_second
        self._bucket = TokenBucket(rate, max(1, math.ceil(rate * 2)))
        self._channel_tails: dict[str, asyncio.Future[None]] = {}
        self._echoes: LruCache[str, str] = LruCache(2000, 5 * 60.0)
        self._sent_at: LruCache[str, float] = LruCache(5000, 60.0)
        self._typing_at: LruCache[str, float] = LruCache(1000, 60.0)
        self._streams: set[StreamControllerImpl] = set()
        self._item_deadline = opts.item_deadline
        self._stream_min_interval = opts.stream_min_interval
        self.interrupted_notice = opts.interrupted_notice

    def note_echo(self, client_msg_id: str, ts: str) -> None:
        """Records a WebSocket echo of a message the SDK sent (dual-channel confirmation)."""
        self._echoes.set(client_msg_id, ts)

    def note_deleted(self, ts: str) -> None:
        """Called by the client for every message.deleted frame (cancels affected streams)."""
        for s in list(self._streams):
            s.notify_deleted(ts)

    @property
    def active_streams(self) -> frozenset[StreamControllerImpl]:
        return frozenset(self._streams)

    async def send(
        self,
        channel: str,
        markdown: str | None = None,
        *,
        blocks: list[Any] | None = None,
        thread_ts: str | None = None,
        broadcast: bool = False,
        priority: Literal["interactive", "normal"] = "normal",
        preserve_unread_cursor: bool = False,
        auto_chunk: bool = True,
        signal: AbortSignal | None = None,
    ) -> SendResult:
        """Posts standard Markdown (sent as ``markdown_text``) or ``blocks`` — exactly one of them.

        Oversized Markdown is split into several messages unless ``auto_chunk=False``;
        ``broadcast`` also posts a thread reply to the channel; ``preserve_unread_cursor``
        restores the channel's unread cursor after sending (best effort).
        """
        channel = assert_channel_id(channel)
        if thread_ts is not None:
            assert_ts(thread_ts, "thread_ts")
        if (markdown is None) == (blocks is None):
            raise GabrielError("INVALID_ARGUMENT", "chat.send: pass exactly one of `markdown` or `blocks`")
        if markdown is not None and (not isinstance(markdown, str) or len(markdown.strip()) == 0):
            raise GabrielError("INVALID_ARGUMENT", "chat.send: `markdown` must be a non-empty string")
        if signal is not None and signal.aborted:
            raise GabrielError("CANCELLED", "context cancelled before send")
        self._hooks.before_auto_write(channel, thread_ts)
        pieces: list[str | None] = list(chunk_markdown(markdown)) if markdown is not None and auto_chunk else [markdown]
        prio: SendPriority = priority

        async def ordered() -> SendResult:
            chunk_ts: list[str] = []
            for piece in pieces:
                msg = _Msg(channel=channel, thread_ts=thread_ts, broadcast=broadcast)
                if piece is not None:
                    msg.markdown = piece
                else:
                    msg.blocks = blocks
                chunk_ts.extend(await self._post_with_resplit(msg, prio, auto_chunk, DEFAULT_CHUNK_BUDGET, signal))
            return SendResult(channel, chunk_ts[0], chunk_ts)

        result = await self._hooks.with_unread_guard(
            channel, preserve_unread_cursor, lambda: self._in_channel_order(channel, ordered)
        )
        if thread_ts:
            self._hooks.note_thread_activity(channel, thread_ts)
        return result

    async def update(
        self, channel: str, ts: str, markdown: str | None = None, *, blocks: list[Any] | None = None
    ) -> UpdateResult:
        channel = assert_channel_id(channel)
        ts = assert_ts(ts)
        if (markdown is None) == (blocks is None):
            raise GabrielError("INVALID_ARGUMENT", "chat.update: pass exactly one of `markdown` or `blocks`")
        await self._raw_update(channel, ts, markdown, blocks, "normal")
        return UpdateResult(channel, ts)

    async def delete(self, channel: str, ts: str) -> None:
        channel = assert_channel_id(channel)
        ts = assert_ts(ts)
        await self._with_not_found_retry(
            ts,
            lambda: self._core.call_web_api(
                "chat.delete", {"channel": channel, "ts": ts}, RequestOpts(reason="delete_message")
            ),
        )

    async def send_typing(self, channel: str, *, thread_ts: str | None = None) -> None:
        """Sends a typing indicator over the WebSocket, throttled to once per 3 s per location."""
        channel = assert_channel_id(channel)
        key = f"{channel}:{thread_ts or ''}"
        last = self._typing_at.get(key)
        now = time.monotonic()
        if last is not None and now - last < 3.0:
            return
        self._typing_at.set(key, now)
        frame: dict[str, Any] = {"type": "user_typing", "channel": channel}
        if thread_ts:
            frame["thread_ts"] = thread_ts
        try:
            self._core.send_ws_frame(frame)
        except Exception as err:
            self._logger.debug({"err": str(err)}, "typing indicator skipped (socket not connected)")

    async def create_stream(
        self,
        channel: str,
        *,
        thread_ts: str | None = None,
        initial_markdown: str | None = None,
        preserve_unread_cursor: bool = False,
        signal: AbortSignal | None = None,
    ) -> StreamController:
        """Posts a placeholder and returns a latest-wins stream controller."""
        channel = assert_channel_id(channel)
        if thread_ts is not None:
            assert_ts(thread_ts, "thread_ts")
        self._hooks.before_auto_write(channel, thread_ts)
        initial = initial_markdown if initial_markdown and initial_markdown.strip() else "…"

        async def post(markdown: str, t_ts: str | None) -> str:
            async def ordered() -> str:
                posted = await self._post_with_resplit(
                    _Msg(channel=channel, markdown=markdown, thread_ts=t_ts),
                    "interactive",
                    False,
                    DEFAULT_CHUNK_BUDGET,
                    signal,
                )
                return posted[0]

            return await self._hooks.with_unread_guard(
                channel, preserve_unread_cursor, lambda: self._in_channel_order(channel, ordered)
            )

        first_ts = await post(initial, thread_ts)
        if thread_ts:
            self._hooks.note_thread_activity(channel, thread_ts)
        chat = self

        class _Transport:
            async def post(self, markdown: str, thread_ts: str | None) -> str:
                return await post(markdown, thread_ts)

            async def update(self, ts: str, markdown: str) -> None:
                await chat._raw_update(channel, ts, markdown, None, "stream_update")

        stream = StreamControllerImpl(
            first_ts,
            StreamOptions(
                channel=channel,
                thread_ts=thread_ts or None,
                budget=DEFAULT_CHUNK_BUDGET,
                min_interval=self._stream_min_interval,
                linked_signal=signal,
            ),
            _Transport(),
            self._bucket,
            self._registry,
            self._logger,
            self._streams.discard,
        )
        self._streams.add(stream)
        return stream

    def stream(
        self,
        channel: str,
        *,
        thread_ts: str | None = None,
        initial_markdown: str | None = None,
        preserve_unread_cursor: bool = False,
        signal: AbortSignal | None = None,
    ) -> _StreamContext:
        """``async with chat.stream(channel, thread_ts=...) as s: s.append(chunk)``.

        Finishes the stream when the block exits normally and aborts it on an exception.
        """
        return _StreamContext(
            lambda: self.create_stream(
                channel,
                thread_ts=thread_ts,
                initial_markdown=initial_markdown,
                preserve_unread_cursor=preserve_unread_cursor,
                signal=signal,
            )
        )

    async def interrupt_all(self) -> None:
        """Interrupts all unfinished streams (graceful shutdown)."""
        await asyncio.gather(*(s.interrupt(self.interrupted_notice) for s in list(self._streams)))

    async def drain(self) -> None:
        """Waits for all queued channel sends to finish."""
        await asyncio.gather(*(asyncio.shield(t) for t in list(self._channel_tails.values())), return_exceptions=True)

    async def _in_channel_order(self, channel: str, fn: Callable[[], Awaitable[T]]) -> T:
        prev = self._channel_tails.get(channel)
        tail: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._channel_tails[channel] = tail
        try:
            if prev is not None:
                await asyncio.shield(prev)
            return await fn()
        finally:

            def complete(_: object = None) -> None:
                if not tail.done():
                    tail.set_result(None)
                if self._channel_tails.get(channel) is tail:
                    del self._channel_tails[channel]

            # Strict FIFO even when this caller was cancelled while waiting for its turn.
            if prev is not None and not prev.done():
                prev.add_done_callback(complete)
            else:
                complete()

    async def _post_with_resplit(
        self, msg: _Msg, priority: SendPriority, auto_chunk: bool, budget: int, signal: AbortSignal | None
    ) -> list[str]:
        try:
            return [await self._post_one(msg, priority, signal)]
        except GabrielError as err:
            if (
                auto_chunk
                and msg.markdown is not None
                and err.code in ("MSG_TOO_LONG", "MSG_BLOCKS_TOO_LONG")
                and budget > 500
            ):
                nxt = int(budget * 0.8)
                pieces = chunk_markdown(msg.markdown, nxt, min(DEFAULT_CHUNK_MIN, int(nxt * 0.7)))
                if len(pieces) == 1 and budget < 2000:
                    raise
                out: list[str] = []
                for p in pieces:
                    sub = _Msg(channel=msg.channel, markdown=p, thread_ts=msg.thread_ts, broadcast=msg.broadcast)
                    out.extend(await self._post_with_resplit(sub, priority, auto_chunk, nxt, signal))
                return out
            raise

    async def _post_one(self, msg: _Msg, priority: SendPriority, signal: AbortSignal | None) -> str:
        client_msg_id = str(uuid.uuid4())
        self._registry.register_client_msg_id(client_msg_id)
        deadline = AbortSignal.timeout(self._item_deadline)
        combined = AbortSignal.any(deadline, signal)
        try:
            release = await self._semaphore.acquire(priority, combined)
        except BaseException:
            combined.dispose()
            deadline.dispose()
            raise
        try:
            fields: dict[str, Any] = {
                "channel": msg.channel,
                "type": "message",
                "client_msg_id": client_msg_id,
                "include_channel_perm_error": True,
                "unfurl": "[]",
            }
            if msg.markdown is not None:
                fields["markdown_text"] = msg.markdown
            else:
                fields["blocks"] = msg.blocks
            if msg.thread_ts:
                fields["thread_ts"] = msg.thread_ts
                if msg.broadcast:
                    fields["reply_broadcast"] = True
            try:
                res = await self._core.call_web_api(
                    "chat.postMessage", fields, RequestOpts(reason="webapp_message_send", signal=combined)
                )
            except GabrielError as err:
                # Dual-channel confirmation: if the WebSocket already delivered our message, the send succeeded.
                echoed = self._echoes.get(client_msg_id)
                if echoed and err.code in ("NETWORK_TIMEOUT", "SERVER_ERROR", "CANCELLED"):
                    self._registry.register_sent_ts(echoed)
                    return echoed
                if deadline.aborted and not (signal is not None and signal.aborted):
                    raise GabrielError("NETWORK_TIMEOUT", "chat.postMessage exceeded item_deadline", cause=err) from err
                raise
            message = res.get("message")
            ts = res.get("ts")
            if ts is None and isinstance(message, dict) and isinstance(message.get("ts"), str):
                ts = message["ts"]
            if not ts or not is_ts(ts):
                raise GabrielError("SLACK_API_ERROR", "chat.postMessage returned no ts")
            self._registry.register_sent_ts(ts)
            if isinstance(message, dict):
                self._registry.register_written_hash(ts, content_hash(message))
            self._sent_at.set(ts, time.time())
            return ts
        finally:
            release()
            combined.dispose()
            deadline.dispose()

    async def _raw_update(
        self, channel: str, ts: str, markdown: str | None, blocks: list[Any] | None, priority: SendPriority
    ) -> None:
        self._registry.register_pending_edit(ts)
        deadline = AbortSignal.timeout(self._item_deadline)
        try:
            release = await self._semaphore.acquire(priority, deadline)
        finally:
            deadline.dispose()
        try:
            fields: dict[str, Any] = {"channel": channel, "ts": ts, "skip_dlp_user_warning": False}
            if markdown is not None:
                fields["markdown_text"] = markdown
            else:
                fields["blocks"] = blocks
            res = await self._with_not_found_retry(
                ts, lambda: self._core.call_web_api("chat.update", fields, RequestOpts(reason="saveMessageEdit"))
            )
            message = res.get("message")
            if isinstance(message, dict):
                self._registry.register_written_hash(ts, content_hash(message))
        finally:
            release()

    async def _with_not_found_retry(self, ts: str, fn: Callable[[], Awaitable[T]]) -> T:
        """Retries ``message_not_found`` every 175 ms (max 10) only within 2 s of the message's creation."""
        created_at = self._sent_at.get(ts)
        if created_at is None:
            created_at = _ts_to_seconds(ts)
        attempt = 0
        while True:
            try:
                return await fn()
            except GabrielError as err:
                if err.raw_slack_error != "message_not_found":
                    raise
                if time.time() - created_at > _CHAT_WINDOW or attempt >= _NOT_FOUND_RETRIES:
                    raise
                await sleep(_NOT_FOUND_RETRY)
            attempt += 1


class _StreamContext:
    def __init__(self, factory: Callable[[], Awaitable[StreamController]]) -> None:
        self._factory = factory
        self._stream: StreamController | None = None

    async def __aenter__(self) -> StreamController:
        self._stream = await self._factory()
        return self._stream

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._stream is not None:
            await self._stream.__aexit__(exc_type, exc, tb)
