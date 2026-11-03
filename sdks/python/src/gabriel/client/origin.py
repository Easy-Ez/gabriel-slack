"""Self-send registry and loop breaker (spec/behavior.md §8)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ..events.types import MessageOrigin
from ..util.lru import LruCache

_SELF_TTL = 5 * 60.0
_EDIT_ECHO_WINDOW = 15.0


@dataclass(slots=True)
class LoopCheck:
    allowed: bool
    count: int


class SelfRegistry:
    """Self-send registry.

    Writes are registered *before* the network request so a fast WebSocket echo can never
    beat the bookkeeping.
    """

    def __init__(self, self_user_id: Callable[[], str], now: Callable[[], float] = time.monotonic) -> None:
        self._self_user_id = self_user_id
        self._now = now
        self._client_msg_ids: LruCache[str, bool] = LruCache(2000, _SELF_TTL, now)
        self._sent_ts: LruCache[str, bool] = LruCache(2000, _SELF_TTL, now)
        #: ts → content hashes the SDK wrote recently.
        self._written_hashes: LruCache[str, list[str]] = LruCache(2000, _SELF_TTL, now)
        #: ts → edit echo window deadline.
        self._pending_edits: LruCache[str, float] = LruCache(2000, _SELF_TTL, now)
        self._active_streams: set[str] = set()
        #: channel → ts values the SDK itself marked as read (conversations.mark echoes).
        self._self_marks: LruCache[str, set[str]] = LruCache(1000, 60.0, now)

    def register_client_msg_id(self, id: str) -> None:
        self._client_msg_ids.set(id, True)

    def register_sent_ts(self, ts: str) -> None:
        self._sent_ts.set(ts, True)

    def register_pending_edit(self, ts: str) -> None:
        """Called before chat.update is sent."""
        self._pending_edits.set(ts, self._now() + _EDIT_ECHO_WINDOW)

    def register_written_hash(self, ts: str, hash: str) -> None:
        lst = self._written_hashes.get(ts) or []
        lst.append(hash)
        if len(lst) > 20:
            lst.pop(0)
        self._written_hashes.set(ts, lst)

    def begin_stream(self, ts: str) -> None:
        self._active_streams.add(ts)

    def end_stream(self, ts: str) -> None:
        self._active_streams.discard(ts)

    def is_active_stream(self, ts: str) -> bool:
        return ts in self._active_streams

    def is_self_sent(self, ts: str) -> bool:
        return self._sent_ts.has(ts) or ts in self._active_streams

    def register_self_mark(self, channel: str, ts: str) -> None:
        marks = self._self_marks.get(channel) or set()
        marks.add(ts)
        self._self_marks.set(channel, marks)

    def is_self_mark(self, channel: str, ts: str) -> bool:
        marks = self._self_marks.get(channel)
        return (marks is not None and ts in marks) or self._sent_ts.has(ts)

    def classify(
        self,
        *,
        ts: str,
        content_hash: str,
        is_edit: bool,
        user: str | None = None,
        bot_id: str | None = None,
        client_msg_id: str | None = None,
    ) -> MessageOrigin:
        if bot_id is not None:
            return "bot"
        if user is None or user != self._self_user_id():
            return "other_user"
        known = (
            (client_msg_id is not None and self._client_msg_ids.has(client_msg_id))
            or self._sent_ts.has(ts)
            or ts in self._active_streams
        )
        if not known:
            return "self_other_client"
        if not is_edit:
            return "this_client"
        if ts in self._active_streams:
            return "this_client"
        hashes = self._written_hashes.get(ts)
        if hashes is not None and content_hash in hashes:
            return "this_client"
        deadline = self._pending_edits.get(ts)
        if deadline is not None and deadline >= self._now():
            return "this_client"
        # The account owner edited a message the SDK had sent.
        return "self_other_client"


class LoopBreaker:
    """Sliding-window auto-reply circuit breaker per (channel, thread)."""

    def __init__(self, max_per_minute: int, now: Callable[[], float] = time.monotonic) -> None:
        self._max = max_per_minute
        self._now = now
        self._windows: dict[str, list[float]] = {}

    def try_record(self, channel: str, thread_ts: str | None) -> LoopCheck:
        """Records an automatic write; reports the window count and whether it was allowed."""
        key = f"{channel}:{thread_ts or '-'}"
        cutoff = self._now() - 60.0
        lst = [t for t in self._windows.get(key, []) if t > cutoff]
        if len(lst) >= self._max:
            self._windows[key] = lst
            return LoopCheck(False, len(lst))
        lst.append(self._now())
        self._windows[key] = lst
        if len(self._windows) > 5000:
            for k, v in list(self._windows.items()):
                if all(t <= cutoff for t in v):
                    del self._windows[k]
        return LoopCheck(True, len(lst))
