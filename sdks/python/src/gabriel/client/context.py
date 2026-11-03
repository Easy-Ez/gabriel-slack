"""Handler context, trigger tracking and the ``wait_for_reply`` router."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..errors import GabrielError
from ..events.types import (
    AiStreamEvent,
    BaseEvent,
    MessageCreatedEvent,
    MessageUpdatedEvent,
    ReactionEvent,
    TypingEvent,
)
from ..util.validate import compare_ts

if TYPE_CHECKING:
    from ..util.aio import AbortSignal
    from .chat import SendResult
    from .stream import StreamController

MessagePredicate = Callable[[MessageCreatedEvent], bool]


@dataclass(kw_only=True, slots=True)
class MessageMatcher:
    #: Channel ID allow-list.
    channel: str | Sequence[str] | None = None
    #: Equivalent to ``evt.is_mentioning_me is True`` (False requires not mentioning me).
    mentions_me: bool | None = None
    #: Tested (``search``) against ``evt.markdown``.
    pattern: re.Pattern[str] | str | None = None


def matcher_to_predicate(flt: MessageMatcher | MessagePredicate) -> MessagePredicate:
    if not isinstance(flt, MessageMatcher):
        return flt
    channels: set[str] | None = None
    if flt.channel is not None:
        channels = {flt.channel} if isinstance(flt.channel, str) else set(flt.channel)
    pattern = re.compile(flt.pattern) if isinstance(flt.pattern, str) else flt.pattern

    def predicate(evt: MessageCreatedEvent) -> bool:
        if channels is not None and evt.channel.id not in channels:
            return False
        if flt.mentions_me is True and not evt.is_mentioning_me:
            return False
        if flt.mentions_me is False and evt.is_mentioning_me:
            return False
        return not (pattern is not None and not pattern.search(evt.markdown))

    return predicate


@dataclass(kw_only=True, slots=True)
class ReplyOptions:
    markdown: str
    #: Default True: reply in the trigger's thread (or start one under the trigger message).
    in_thread: bool = True
    broadcast: bool = False
    preserve_unread_cursor: bool = False


class EventContext(Protocol):
    """Handler context (``ctx``)."""

    #: Aborted when the triggering message is deleted, the handler exceeds ``handler_timeout``,
    #: or ``client.stop()`` drain time runs out.
    @property
    def signal(self) -> AbortSignal: ...

    async def reply(
        self,
        markdown: str | ReplyOptions,
        *,
        in_thread: bool = True,
        broadcast: bool = False,
        preserve_unread_cursor: bool = False,
    ) -> SendResult: ...

    async def reply_stream(
        self, *, initial_markdown: str | None = None, preserve_unread_cursor: bool = False
    ) -> StreamController: ...

    async def react(self, emoji: str) -> None: ...

    async def wait_for_reply(
        self,
        flt: MessageMatcher | MessagePredicate,
        *,
        timeout: float,
        consume: bool = True,
        exclusive: bool = False,
    ) -> MessageCreatedEvent | None: ...

    async def hydrate_remaining(self) -> None: ...


@dataclass(kw_only=True, slots=True)
class TriggerInfo:
    """Ambient trigger of the running handler (scope sandbox + loop breaker)."""

    event_id: str
    channel: str | None = None
    #: Thread the handler replies into: trigger's thread root, or the trigger ts itself.
    thread_root_ts: str | None = None
    ts: str | None = None


_trigger_var: ContextVar[TriggerInfo | None] = ContextVar("gabriel_trigger", default=None)


def current_trigger() -> TriggerInfo | None:
    return _trigger_var.get()


def set_trigger(info: TriggerInfo | None) -> Any:
    """Sets the ambient trigger for the current context; returns a token for :func:`reset_trigger`."""
    return _trigger_var.set(info)


def reset_trigger(token: Any) -> None:
    _trigger_var.reset(token)


def trigger_of(evt: BaseEvent) -> TriggerInfo:
    """Derives where a reply to this event should go."""
    if isinstance(evt, MessageCreatedEvent):
        root = evt.thread.root_ts if evt.thread is not None else evt.ts
        return TriggerInfo(channel=evt.channel.id, thread_root_ts=root, ts=evt.ts, event_id=evt.event_id)
    if isinstance(evt, MessageUpdatedEvent | AiStreamEvent):
        return TriggerInfo(
            channel=evt.channel.id, thread_root_ts=evt.thread_ts or evt.ts, ts=evt.ts, event_id=evt.event_id
        )
    if isinstance(evt, ReactionEvent):
        info = TriggerInfo(channel=evt.channel.id or None, event_id=evt.event_id)
        if evt.target.ts:
            info.thread_root_ts = evt.target.thread_root_ts or evt.target.ts
            info.ts = evt.target.ts
        return info
    if isinstance(evt, TypingEvent):
        return TriggerInfo(channel=evt.channel.id, thread_root_ts=evt.thread_root_ts or None, event_id=evt.event_id)
    ch = getattr(evt, "channel", None)
    ch_id = getattr(ch, "id", None)
    return TriggerInfo(channel=ch_id or None, event_id=evt.event_id)


@dataclass(slots=True, eq=False)
class Waiter:
    predicate: MessagePredicate
    consume: bool
    exclusive: bool
    resolve: Callable[[MessageCreatedEvent], None]
    after_ts: str | None = None


_RECENT_WINDOW = 10.0


class WaitRegistry:
    """Pre-executor reply router for ``ctx.wait_for_reply`` (spec/behavior.md §6.3).

    Matching happens after hydration and before the executor queue, so replies reach waiters
    even when the queue is full.
    """

    def __init__(self, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._waiters: list[Waiter] = []
        self._recent: list[tuple[MessageCreatedEvent, float]] = []

    def route(self, evt: BaseEvent) -> bool:
        """Returns True when the event was consumed by a waiter."""
        if not isinstance(evt, MessageCreatedEvent):
            return False
        consumed = False
        delivered = False
        i = 0
        while i < len(self._waiters):
            w = self._waiters[i]
            if (w.after_ts and compare_ts(evt.ts, w.after_ts) <= 0) or not _safe_test(w.predicate, evt):
                i += 1
                continue
            if w.exclusive and delivered:
                i += 1
                continue
            self._waiters.pop(i)
            w.resolve(evt)
            delivered = True
            if w.consume:
                consumed = True
            if w.exclusive:
                break
        if not consumed:
            self._remember(evt)
        return consumed

    def _remember(self, evt: MessageCreatedEvent) -> None:
        now = self._now()
        self._recent = [r for r in self._recent if now - r[1] < _RECENT_WINDOW]
        self._recent.append((evt, now))
        if len(self._recent) > 500:
            self._recent.pop(0)

    def take_recent(
        self, predicate: MessagePredicate, after_ts: str | None, consume: bool
    ) -> MessageCreatedEvent | None:
        """Scans recently routed, unconsumed messages (newer than ``after_ts``)."""
        now = self._now()
        for idx, (evt, at) in enumerate(self._recent):
            if (
                now - at < _RECENT_WINDOW
                and (not after_ts or compare_ts(evt.ts, after_ts) > 0)
                and _safe_test(predicate, evt)
            ):
                if consume:
                    self._recent.pop(idx)
                return evt
        return None

    def add(self, waiter: Waiter) -> Callable[[], None]:
        self._waiters.append(waiter)

        def remove() -> None:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

        return remove


def _safe_test(predicate: MessagePredicate, evt: MessageCreatedEvent) -> bool:
    try:
        return bool(predicate(evt))
    except Exception:
        return False


def assert_writable(cancelled: bool) -> None:
    if cancelled:
        raise GabrielError("CANCELLED", "handler timed out or was cancelled; writes through this ctx are rejected")
