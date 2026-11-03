"""``preserve_unread_cursor`` support (spec/behavior.md §9)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from ..core.types import RequestOpts
from ..util.logger import Logger
from ..util.validate import compare_ts, is_ts

if TYPE_CHECKING:
    from ..core.core import GabrielCore
    from .origin import SelfRegistry

T = TypeVar("T")


@dataclass(slots=True)
class _Baseline:
    last_read: str
    has_unread: bool


@dataclass(slots=True, eq=False)
class _ChannelGuard:
    baseline: asyncio.Future[_Baseline | None]
    inflight: int = 0
    user_read: bool = False
    last_read: str | None = None
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


class UnreadCursorGuard:
    """Posting a message advances the channel's ``last_read``.

    The baseline is latched when the first concurrent post starts and restored once after the
    last one finishes — unless the account owner reads the channel meanwhile. Best effort:
    ``conversations.mark`` has no compare-and-swap.
    """

    def __init__(self, core: GabrielCore, registry: SelfRegistry, logger: Logger) -> None:
        self._core = core
        self._registry = registry
        self._logger = logger
        self._channels: dict[str, _ChannelGuard] = {}

    async def run(self, channel: str, enabled: bool, fn: Callable[[], Awaitable[T]]) -> T:
        if not enabled:
            return await fn()
        g = self._channels.get(channel)
        if g is None:
            created = _ChannelGuard(baseline=asyncio.ensure_future(self._fetch_baseline(channel)))

            def latch(fut: asyncio.Future[_Baseline | None]) -> None:
                if not fut.cancelled() and fut.exception() is None and (b := fut.result()) is not None:
                    created.last_read = b.last_read

            created.baseline.add_done_callback(latch)
            self._channels[channel] = g = created
        g.inflight += 1
        guard = g
        try:
            await asyncio.shield(guard.baseline)  # latch before the first post advances the cursor
            return await fn()
        finally:
            guard.inflight -= 1
            if guard.inflight == 0:
                await self._restore(channel, guard)

    def on_marked(self, channel: str, ts: str) -> None:
        """Feed of channel_marked-style frames."""
        g = self._channels.get(channel)
        if g is None or not g.last_read:
            return
        if self._registry.is_self_mark(channel, ts):
            return
        if compare_ts(ts, g.last_read) > 0:
            g.user_read = True

    async def _fetch_baseline(self, channel: str) -> _Baseline | None:
        try:
            res = await self._core.call_web_api(
                "conversations.info", {"channel": channel}, RequestOpts(reason="gabriel-unread-baseline")
            )
            ch = res.get("channel") or {}
            last_read = ch.get("last_read")
            last_read = last_read if is_ts(last_read) else None
            unread = any(
                isinstance(v, int | float) and not isinstance(v, bool) and v > 0
                for v in (ch.get("unread_count"), ch.get("unread_count_display"))
            )
            return _Baseline(last_read, unread) if last_read else None
        except Exception as err:
            self._logger.debug({"err": str(err), "channel": channel}, "unread baseline unavailable")
            return None

    async def _restore(self, channel: str, g: _ChannelGuard) -> None:
        try:
            base = await asyncio.shield(g.baseline)
            if base is None or not base.has_unread or g.user_read:
                return
            self._registry.register_self_mark(channel, base.last_read)
            await self._core.call_web_api(
                "conversations.mark",
                {"channel": channel, "ts": base.last_read},
                RequestOpts(reason="gabriel-preserve-unread"),
            )
        except Exception as err:
            self._logger.warn({"err": str(err), "channel": channel}, "could not restore unread cursor")
        finally:
            if self._channels.get(channel) is g and g.inflight == 0:
                del self._channels[channel]
