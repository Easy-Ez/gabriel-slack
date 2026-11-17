"""Watermarks and gap recovery (spec/behavior.md §10)."""

from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from ..core.types import RawWsFrame, RequestOpts
from ..errors import GabrielError
from ..events.types import GapRange, GapThread
from ..state.store import StateStore
from ..util.logger import Logger
from ..util.validate import compare_ts, is_ts, max_ts

if TYPE_CHECKING:
    from ..core.core import GabrielCore
    from .apis import ChannelsApi


@dataclass(slots=True)
class _Lane:
    inflight: set[str] = field(default_factory=set)
    pinned: set[str] = field(default_factory=set)
    completed: set[str] = field(default_factory=set)
    wm: str | None = None
    dirty: bool = False


@dataclass(slots=True)
class ThreadWatermark:
    channel: str
    thread_ts: str
    wm: str


def _min_of(sets: Iterable[set[str]]) -> str | None:
    found: str | None = None
    for s in sets:
        for ts in s:
            if found is None or compare_ts(ts, found) < 0:
                found = ts
    return found


class WatermarkTracker:
    """Sliding low-watermarks.

    Advanced only by ``message.created``, held back by in-flight and overflow-pinned events,
    persisted with max-merge.
    """

    def __init__(
        self,
        store: StateStore,
        team_id: Callable[[], str],
        watched_threads_limit: int,
        logger: Logger,
        max_explicit_thread_watches: int = 500,
    ) -> None:
        self._store = store
        self._team_id = team_id
        self._watched_threads_limit = watched_threads_limit
        self._max_explicit = max_explicit_thread_watches
        self._logger = logger
        self._lanes: dict[str, _Lane] = {}
        # Activity list: threads this client wrote in or subscribed to (bounded LRU).
        self._watched_threads: dict[str, tuple[str, str]] = {}
        # Explicit list: local-only ``threads.watch()``; never evicted by the LRU.
        self._explicit_threads: dict[str, tuple[str, str]] = {}
        # Thread watermark keys to delete from the store on the next flush.
        self._dropped_thread_marks: set[str] = set()
        self._flush_timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

    def _lane(self, key: str) -> _Lane:
        lane = self._lanes.get(key)
        if lane is None:
            lane = self._lanes[key] = _Lane()
        return lane

    def _keys_for(self, channel: str, thread_ts: str | None, thread_only: bool = False) -> list[str]:
        """``thread_only``: replies fetched directly from a thread move only the thread's lane."""
        keys = [] if thread_only else [f"c:{channel}"]
        if thread_ts and self.is_thread_watched(channel, thread_ts):
            keys.append(f"t:{channel}:{thread_ts}")
        return keys

    def begin(self, channel: str, ts: str, thread_ts: str | None = None, thread_only: bool = False) -> None:
        for k in self._keys_for(channel, thread_ts, thread_only):
            self._lane(k).inflight.add(ts)

    def complete(self, channel: str, ts: str, thread_ts: str | None = None, thread_only: bool = False) -> None:
        for k in self._keys_for(channel, thread_ts, thread_only):
            lane = self._lane(k)
            lane.inflight.discard(ts)
            lane.completed.add(ts)
            self._advance(lane)
        self._schedule_flush()

    def pin(self, channel: str, ts: str, thread_ts: str | None = None, thread_only: bool = False) -> None:
        """Overflow: keeps the watermark below ``ts`` until a gap recovery re-delivers it."""
        for k in self._keys_for(channel, thread_ts, thread_only):
            lane = self._lane(k)
            lane.inflight.discard(ts)
            lane.pinned.add(ts)

    def unpin_all(self, channel: str) -> None:
        """Clears overflow pins of a channel and of its watched threads (after a recovery refetched them)."""
        for key, lane in self._lanes.items():
            if key != f"c:{channel}" and not key.startswith(f"t:{channel}:"):
                continue
            if not lane.pinned:
                continue
            lane.pinned.clear()
            self._advance(lane)

    def has_pinned(self) -> bool:
        return any(lane.pinned for lane in self._lanes.values())

    def _advance(self, lane: _Lane) -> None:
        min_pending = _min_of([lane.inflight, lane.pinned])
        best: str | None = None
        for ts in list(lane.completed):
            if min_pending is None or compare_ts(ts, min_pending) < 0:
                best = max_ts(best, ts)
                lane.completed.discard(ts)
        if best is not None and (lane.wm is None or compare_ts(best, lane.wm) > 0):
            lane.wm = best
            lane.dirty = True

    def channel_watermarks(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, lane in self._lanes.items():
            if not k.startswith("c:"):
                continue
            # Pinned (dropped) events force recovery from just before the earliest drop.
            pinned_min = _min_of([lane.pinned])
            wm = lane.wm
            if wm is not None:
                out[k[2:]] = pinned_min if pinned_min is not None and compare_ts(pinned_min, wm) <= 0 else wm
        return out

    def thread_watermarks(self) -> list[ThreadWatermark]:
        out: list[ThreadWatermark] = []
        seen: set[str] = set()
        for threads in (self._explicit_threads, self._watched_threads):
            for key, (channel, thread_ts) in threads.items():
                if key in seen:
                    continue
                seen.add(key)
                out.append(ThreadWatermark(channel, thread_ts, self.thread_watermark(channel, thread_ts)))
        return out

    def thread_watermark(self, channel: str, thread_ts: str) -> str:
        lane = self._lanes.get(f"t:{channel}:{thread_ts}")
        return (lane.wm if lane is not None else None) or thread_ts

    def watch_thread(self, channel: str, thread_ts: str) -> None:
        """Adds a thread to the bounded LRU of threads whose replies are recovered directly."""
        key = f"{channel}:{thread_ts}"
        self._watched_threads.pop(key, None)
        self._watched_threads[key] = (channel, thread_ts)
        while len(self._watched_threads) > self._watched_threads_limit:
            oldest = next(iter(self._watched_threads))
            del self._watched_threads[oldest]
            self._drop_thread_lane(oldest)
        self._schedule_flush()

    def unwatch_thread(self, channel: str, thread_ts: str) -> None:
        key = f"{channel}:{thread_ts}"
        self._watched_threads.pop(key, None)
        self._drop_thread_lane(key)
        self._schedule_flush()

    def watch_explicit(self, channel: str, thread_ts: str, after: str | None = None) -> None:
        """Adds a thread to the explicit (local-only) list.

        Its watermark becomes the newer of the current one and ``after`` (default: the root).
        Raises ``SUBSCRIPTION_CAP_EXCEEDED`` when a new thread exceeds the bound.
        """
        key = f"{channel}:{thread_ts}"
        if key not in self._explicit_threads and len(self._explicit_threads) >= self._max_explicit:
            raise GabrielError(
                "SUBSCRIPTION_CAP_EXCEEDED",
                f"explicit thread watches would reach {len(self._explicit_threads) + 1} (> {self._max_explicit})",
            )
        self._explicit_threads[key] = (channel, thread_ts)
        lane = self._lane(f"t:{key}")
        initial = max_ts(thread_ts, after) or thread_ts
        if lane.wm is None or compare_ts(initial, lane.wm) > 0:
            lane.wm = initial
            lane.dirty = True
        self._schedule_flush()

    def unwatch_explicit(self, channel: str, thread_ts: str) -> None:
        key = f"{channel}:{thread_ts}"
        if self._explicit_threads.pop(key, None) is None:
            return
        self._drop_thread_lane(key)
        self._schedule_flush()

    def explicit_watches(self) -> list[tuple[str, str, str]]:
        """``(channel, thread_ts, watermark)`` of every explicitly watched thread."""
        return [(c, t, self.thread_watermark(c, t)) for c, t in self._explicit_threads.values()]

    def is_explicitly_watched(self, channel: str, thread_ts: str) -> bool:
        return f"{channel}:{thread_ts}" in self._explicit_threads

    def is_thread_watched(self, channel: str, thread_ts: str) -> bool:
        key = f"{channel}:{thread_ts}"
        return key in self._watched_threads or key in self._explicit_threads

    def unpin_thread(self, channel: str, thread_ts: str) -> None:
        """Clears overflow pins of one thread lane after its replies were refetched."""
        lane = self._lanes.get(f"t:{channel}:{thread_ts}")
        if lane is None or not lane.pinned:
            return
        lane.pinned.clear()
        self._advance(lane)

    def _drop_thread_lane(self, key: str) -> None:
        """Forgets a thread's lane and stored watermark once neither list holds the thread."""
        if key in self._watched_threads or key in self._explicit_threads:
            return
        self._lanes.pop(f"t:{key}", None)
        self._dropped_thread_marks.add(key)

    async def load(self) -> None:
        prefix = f"gabriel:wm:{self._team_id()}:"
        for key in await self._store.keys(prefix):
            value = await self._store.get(key)
            if not value or not is_ts(value):
                continue
            lane = self._lane(key[len(prefix) :])
            lane.wm = max_ts(lane.wm, value)
        raw = await self._store.get(f"gabriel:threads:{self._team_id()}")
        if raw:
            try:
                for t in json.loads(raw):
                    channel, thread_ts = t.get("channel"), t.get("threadTs")
                    if isinstance(channel, str) and is_ts(thread_ts):
                        self._watched_threads[f"{channel}:{thread_ts}"] = (channel, thread_ts)
            except (ValueError, AttributeError, TypeError):
                self._logger.warn({}, "ignoring corrupt watched-thread list")
        explicit = await self._store.get(f"gabriel:watches:{self._team_id()}")
        if explicit:
            try:
                for t in json.loads(explicit):
                    channel, thread_ts = t.get("channel"), t.get("threadTs")
                    if isinstance(channel, str) and is_ts(thread_ts):
                        self._explicit_threads[f"{channel}:{thread_ts}"] = (channel, thread_ts)
            except (ValueError, AttributeError, TypeError):
                self._logger.warn({}, "ignoring corrupt explicit thread-watch list")

    def _schedule_flush(self) -> None:
        if self._flush_timer is not None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        def fire() -> None:
            self._flush_timer = None
            self._tasks.add(task := asyncio.ensure_future(self._flush_logged()))
            task.add_done_callback(self._tasks.discard)

        self._flush_timer = loop.call_later(1.0, fire)

    async def _flush_logged(self) -> None:
        try:
            await self.flush()
        except Exception as err:
            self._logger.warn({"err": str(err)}, "watermark flush failed")

    async def flush(self) -> None:
        if self._flush_timer is not None:
            self._flush_timer.cancel()
            self._flush_timer = None
        team = self._team_id()
        # Deletions first: a thread dropped and watched again starts from its new watermark.
        dropped = list(self._dropped_thread_marks)
        self._dropped_thread_marks.clear()
        await asyncio.gather(*(self._store.delete(f"gabriel:wm:{team}:t:{key}") for key in dropped))
        writes: list[Any] = []
        for k, lane in self._lanes.items():
            if not lane.dirty or lane.wm is None:
                continue
            lane.dirty = False
            writes.append(self._store.max_merge_ts(f"gabriel:wm:{team}:{k}", lane.wm))
        # Same JSON shape as the Node SDK so a shared state file stays readable by both.
        threads = [{"channel": c, "threadTs": t} for c, t in self._watched_threads.values()]
        writes.append(self._store.set(f"gabriel:threads:{team}", json.dumps(threads, separators=(",", ":"))))
        explicit = [{"channel": c, "threadTs": t} for c, t in self._explicit_threads.values()]
        writes.append(self._store.set(f"gabriel:watches:{team}", json.dumps(explicit, separators=(",", ":"))))
        await asyncio.gather(*writes)


@dataclass(kw_only=True, slots=True)
class GapSummary:
    """A :class:`~gabriel.events.types.ConnectionGapEvent` without ``event_id`` and ``cause``."""

    recovered: bool = True
    recovered_count: int = 0
    channels_recovered: list[str] = field(default_factory=list)
    threads_recovered: list[GapThread] = field(default_factory=list)
    truncated: bool = False
    unrecovered: list[GapRange] = field(default_factory=list)
    from_ts: str | None = None
    to_ts: str | None = None


@dataclass(slots=True)
class RecoveryResult:
    frames: list[RawWsFrame]
    gap: GapSummary


def _reason_of(err: BaseException) -> Literal["rate_limited", "api_error"]:
    return "rate_limited" if getattr(err, "code", None) == "RATE_LIMITED" else "api_error"


@dataclass(slots=True)
class ThreadFetchResult:
    """Replies newer than the watermark (root excluded) and why the fetch stopped early, if it did."""

    frames: list[RawWsFrame]
    failure: Literal["page_limit", "rate_limited", "api_error"] | None = None


async def fetch_thread_replies(core: GabrielCore, t: ThreadWatermark, max_pages: int) -> ThreadFetchResult:
    """``conversations.replies(oldest=wm, inclusive=false)``, bounded pages. Never raises."""
    frames: list[RawWsFrame] = []
    cursor: str | None = None
    pages = 0
    try:
        while True:
            fields: dict[str, Any] = {
                "channel": t.channel,
                "ts": t.thread_ts,
                "oldest": t.wm,
                "inclusive": False,
                "limit": 100,
                "include_tombstones": True,
            }
            if cursor:
                fields["cursor"] = cursor
            res = await core.call_web_api(
                "conversations.replies", fields, RequestOpts(reason="history-api/fetchReplies")
            )
            pages += 1
            for m in res.get("messages") or []:
                ts = m.get("ts") if isinstance(m, dict) else None
                if not isinstance(ts, str) or ts == t.thread_ts or compare_ts(ts, t.wm) <= 0:
                    continue
                frames.append({**m, "type": "message", "channel": t.channel})
            meta = res.get("response_metadata")
            next_cursor = meta.get("next_cursor") if isinstance(meta, dict) else None
            cursor = (next_cursor or None) if res.get("has_more") else None
            if cursor and pages >= max_pages:
                return ThreadFetchResult(frames, "page_limit")
            if not cursor:
                return ThreadFetchResult(frames)
    except Exception as err:
        return ThreadFetchResult(frames, _reason_of(err))


async def recover_gap(
    core: GabrielCore,
    channels: ChannelsApi,
    marks: WatermarkTracker,
    *,
    max_pages_per_channel: int,
    logger: Logger,
) -> RecoveryResult:
    """Gap recovery.

    ``client.counts`` → per-channel ``conversations.history(oldest=wm)`` paged towards older
    messages, plus direct ``conversations.replies`` for watched threads (plain thread replies
    never appear in channel history).
    """
    frames: list[RawWsFrame] = []
    gap = GapSummary()
    wms = marks.channel_watermarks()
    if not wms and not marks.thread_watermarks():
        return RecoveryResult(frames, gap)

    latest_by_channel: dict[str, str] = {}
    try:
        counts = await core.call_web_api("client.counts", {}, RequestOpts(reason="gabriel-gap-recovery"))
        for group in ("channels", "mpims", "ims"):
            for c in counts.get(group) or []:
                if isinstance(c, dict) and c.get("id") and is_ts(c.get("latest")):
                    latest_by_channel[c["id"]] = c["latest"]
    except Exception as err:
        logger.warn({"err": str(err)}, "client.counts failed; probing every tracked channel")
        latest_by_channel = {}

    min_from: str | None = None
    max_to: str | None = None

    for channel, wm in wms.items():
        latest = latest_by_channel.get(channel)
        if latest_by_channel and (latest is None or compare_ts(latest, wm) <= 0):
            continue
        cursor: str | None = None
        pages = 0
        got = 0
        try:
            while True:
                page = await channels.raw_history(channel, oldest=wm, inclusive=False, limit=100, cursor=cursor)
                pages += 1
                for m in page.messages:
                    ts = m.get("ts")
                    if not isinstance(ts, str) or compare_ts(ts, wm) <= 0:
                        continue
                    frames.append({**m, "type": "message", "channel": channel})
                    got += 1
                    max_to = max_ts(max_to, ts)
                cursor = page.next_cursor if page.has_more else None
                if cursor and pages >= max_pages_per_channel:
                    gap.truncated = True
                    gap.unrecovered.append(GapRange(channel=channel, from_ts=wm, reason="page_limit"))
                    break
                if not cursor:
                    break
            if got > 0:
                gap.channels_recovered.append(channel)
            min_from = wm if min_from is None or compare_ts(wm, min_from) < 0 else min_from
        except Exception as err:
            gap.unrecovered.append(GapRange(channel=channel, from_ts=wm, reason=_reason_of(err)))

    for t in marks.thread_watermarks():
        res = await fetch_thread_replies(core, t, max_pages_per_channel)
        frames.extend(res.frames)
        if res.frames and res.failure in (None, "page_limit"):
            gap.threads_recovered.append(GapThread(channel=t.channel, thread_ts=t.thread_ts))
        if res.failure:
            if res.failure == "page_limit":
                gap.truncated = True
            gap.unrecovered.append(GapRange(channel=t.channel, thread_ts=t.thread_ts, from_ts=t.wm, reason=res.failure))

    frames.sort(key=functools.cmp_to_key(lambda a, b: compare_ts(str(a.get("ts")), str(b.get("ts")))))
    gap.recovered_count = len(frames)
    gap.recovered = len(gap.unrecovered) == 0
    if min_from:
        gap.from_ts = min_from
    if max_to:
        gap.to_ts = max_to
    return RecoveryResult(frames, gap)
