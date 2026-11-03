"""High-level semantic client: ordered, hydrated, loop-guarded events and domain APIs."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Literal, TypeVar, overload

import httpx

from ..core.auth import AuthDead, AuthEvent, AuthOptions
from ..core.core import GabrielCore, ProtocolDriftInfo
from ..core.metadata import ClientMetadata, Endpoints
from ..core.types import MetricsSink, RawWsFrame, WebSocketFactory, noop_metrics
from ..core.ws import HelloInfo, ReconnectingInfo, WsState, WsTimings
from ..errors import GabrielError, cancelled_error
from ..events.normalize import Draft, EntityNeeds, NormalizeContext, OriginInfo, badge_snapshot_key, normalize_frame
from ..events.types import (
    AuthDeadEvent,
    AuthRefreshedEvent,
    BaseEvent,
    ConnectionGapEvent,
    ConnectionReadyEvent,
    ConnectionReconnectingEvent,
    LoopDetectedEvent,
    MessageCreatedEvent,
    MessageOtherEvent,
    MessageUpdatedEvent,
    OverflowEvent,
    ProtocolDriftEvent,
    UnreadBadgeCountsEvent,
    UserSubteamUpdatedEvent,
    event_type_of,
)
from ..state.store import MemoryStateStore, StateStore
from ..util.aio import AbortController, AbortSignal, spawn
from ..util.logger import Logger, stdlib_logger
from ..util.lru import LruCache
from ..util.validate import compare_ts, is_subteam_id
from .apis import ChannelsApi, ReactionsApi, SearchApi, ThreadsApi, UsersApi
from .chat import ChatApi, ChatSchedulerOptions, SendResult
from .context import (
    MessageMatcher,
    MessagePredicate,
    ReplyOptions,
    Waiter,
    WaitRegistry,
    assert_writable,
    current_trigger,
    matcher_to_predicate,
    reset_trigger,
    set_trigger,
    trigger_of,
)
from .executor import JobHandle, KeyedExecutor
from .files import FilesApi
from .hydrator import HydrationResult, Hydrator, UsergroupRecord
from .origin import LoopBreaker, SelfRegistry
from .presence import PresenceApi
from .recovery import WatermarkTracker, recover_gap
from .scope import ScopedClient, ScopePolicy
from .stream import StreamController
from .unread import UnreadCursorGuard

E = TypeVar("E", bound=BaseEvent)
T = TypeVar("T")

Handler = Callable[[Any, "EventContextImpl"], Awaitable[None] | None]
Middleware = Callable[[BaseEvent, "EventContextImpl", Callable[[], Awaitable[None]]], Awaitable[None]]
ErrorHandler = Callable[[BaseException, BaseEvent | None], None]
EventSelector = str | type[BaseEvent]
H = TypeVar("H", bound=Callable[..., Any])


@dataclass(kw_only=True, slots=True)
class ConcurrencyOptions:
    #: Handlers running at once across all keys (default 32).
    global_concurrency: int = 32
    #: Handlers running at once per executor key; 1 = strictly serial (default 1).
    per_key_concurrency: int = 1
    #: Executor queue bound (default 5000).
    max_queue: int = 5000
    #: In-flight event bound (hydration + queued + running). Excess is dropped with guard.overflow.
    max_inflight_events: int = 10_000
    #: ``channel`` (default): serial per channel; ``thread``: serial per thread, parallel across threads.
    executor_key: Literal["channel", "thread"] = "channel"
    #: Per-handler time limit in seconds, 0 disables (default 300). wait_for_reply time is not counted.
    handler_timeout: float = 300.0


@dataclass(kw_only=True, slots=True)
class HydrationOptions:
    #: Time budget for resolving names per event, in seconds (default 0.3).
    hydrate_budget: float = 0.3
    #: Preload user groups so @group mentions resolve and ``is_mentioning_me`` covers my groups.
    preload_usergroups: bool = True


@dataclass(kw_only=True, slots=True)
class LoopGuardOptions:
    #: Deliver echoes of this client's own writes (``origin == "this_client"``). Default False.
    include_self_messages: bool = False
    #: Automatic writes allowed per (channel, thread) per 60 s (default 5).
    max_auto_replies_per_minute: int = 5


@dataclass(kw_only=True, slots=True)
class RecoveryOptions:
    #: History pages fetched per channel during gap recovery (default 10).
    max_pages_per_channel: int = 10
    #: Threads whose replies are recovered directly (LRU, default 100).
    watched_threads_limit: int = 100
    #: Max realtime frames buffered while resyncing (default 10000).
    resync_buffer_limit: int = 10_000


@dataclass(kw_only=True, slots=True)
class AdvancedOptions:
    """Client identity, endpoints and transport injection (proxies, tests)."""

    metadata: ClientMetadata | Mapping[str, str] | None = None
    endpoints: Endpoints | Mapping[str, str | None] | None = None
    http_client: httpx.AsyncClient | None = None
    ws_factory: WebSocketFactory | None = None
    gateway_server: str | None = None
    #: Seconds ``start()`` waits for the first ``connection.ready`` (default 60).
    start_timeout: float = 60.0
    #: WebSocket protocol timings (tests only).
    ws_timings: WsTimings | None = None


@dataclass(kw_only=True, slots=True)
class HealthReport:
    status: Literal["healthy", "degraded", "dead"]
    ws_state: WsState
    auth_state: str
    #: Seconds since the last WebSocket frame.
    last_frame_age: float
    queue_depth: int


@dataclass(slots=True)
class _Routed:
    draft: Draft
    hydration: HydrationResult


KNOWN_MESSAGE_SUBTYPES: frozenset[str] = frozenset(
    {
        "file_share", "thread_broadcast", "bot_message", "me_message", "message_changed", "message_deleted",
        "message_replied", "channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name",
        "channel_archive", "channel_unarchive", "channel_convert_to_private", "channel_convert_to_public",
        "group_join", "group_leave", "group_topic", "group_purpose", "group_name", "group_archive",
        "group_unarchive", "pinned_item", "unpinned_item", "bot_add", "bot_remove", "tombstone", "reminder_add",
        "huddle_thread", "sh_room_created", "joiner_notification", "file_comment", "ekm_access_denied",
        "slackbot_response", "assistant_app_thread", "channel_canvas_updated", "bot_enable", "bot_disable",
    }
)  # fmt: skip

_CONTROL_TYPES = frozenset({"hello", "pong", "reconnect_url", "goodbye", "error"})

_COMPLETE_HYDRATION = HydrationResult(True)


class _JobControl:
    """Permit control handed to ``wait_for_reply`` (pauses the handler timer while suspended)."""

    def __init__(self, suspend: Callable[[], None], resume: Callable[[], Awaitable[None]]) -> None:
        self.suspend = suspend
        self.resume = resume


_NOOP_JOB = _JobControl(lambda: None, lambda: asyncio.sleep(0))


class EventContextImpl:
    """Handler context (``ctx``)."""

    def __init__(
        self,
        client: GabrielClient,
        evt: BaseEvent,
        controller: AbortController,
        hydration: HydrationResult,
        writes_cancelled: Callable[[], bool],
        job: _JobControl,
    ) -> None:
        self._client = client
        self._evt = evt
        self._controller = controller
        self._hydration = hydration
        self._writes_cancelled = writes_cancelled
        self._job = job
        self._trigger = trigger_of(evt)

    @property
    def signal(self) -> AbortSignal:
        """Aborted when the triggering message is deleted, the handler exceeds ``handler_timeout``,
        or ``client.stop()`` drain time runs out."""
        return self._controller.signal

    @property
    def event(self) -> BaseEvent:
        return self._evt

    def _where(self, in_thread: bool) -> tuple[str, str | None]:
        if not self._trigger.channel:
            raise GabrielError("INVALID_STATE", f"{self._evt.type} has no channel to reply to")
        return self._trigger.channel, (self._trigger.thread_root_ts if in_thread else None)

    def _assert_writable(self) -> None:
        assert_writable(self._writes_cancelled() or self.signal.aborted)

    async def reply(
        self,
        markdown: str | ReplyOptions,
        *,
        in_thread: bool = True,
        broadcast: bool = False,
        preserve_unread_cursor: bool = False,
    ) -> SendResult:
        """Replies in the trigger's thread (or starts one under the trigger message)."""
        self._assert_writable()
        if isinstance(markdown, ReplyOptions):
            opts = markdown
        else:
            opts = ReplyOptions(
                markdown=markdown,
                in_thread=in_thread,
                broadcast=broadcast,
                preserve_unread_cursor=preserve_unread_cursor,
            )
        channel, thread_ts = self._where(opts.in_thread)
        return await self._client.chat.send(
            channel,
            opts.markdown,
            thread_ts=thread_ts,
            priority="interactive",
            broadcast=opts.broadcast,
            preserve_unread_cursor=opts.preserve_unread_cursor,
            signal=self.signal,
        )

    async def reply_stream(
        self, *, initial_markdown: str | None = None, preserve_unread_cursor: bool = False
    ) -> StreamController:
        self._assert_writable()
        channel, thread_ts = self._where(True)
        return await self._client.chat.create_stream(
            channel,
            thread_ts=thread_ts,
            initial_markdown=initial_markdown,
            preserve_unread_cursor=preserve_unread_cursor,
            signal=self.signal,
        )

    async def react(self, emoji: str) -> None:
        self._assert_writable()
        if not self._trigger.channel or not self._trigger.ts:
            raise GabrielError("INVALID_STATE", f"{self._evt.type} has no message to react to")
        await self._client.reactions.add(self._trigger.channel, self._trigger.ts, emoji)

    async def wait_for_reply(
        self,
        flt: MessageMatcher | MessagePredicate,
        *,
        timeout: float,
        consume: bool = True,
        exclusive: bool = False,
    ) -> MessageCreatedEvent | None:
        """Waits for a matching message after the trigger (``None`` on timeout).

        ``consume`` (default True): a matched reply goes only to this waiter, not to other
        handlers. ``exclusive``: this waiter receives a matched reply alone.
        """
        return await self._client._wait_for_reply(self._evt, flt, timeout, consume, exclusive, self.signal, self._job)

    async def hydrate_remaining(self) -> None:
        await self._hydration.remaining()


class _PullQueue:
    def __init__(self, client: GabrielClient, limit: int) -> None:
        self._client = client
        self._limit = limit
        self._buffer: deque[BaseEvent] = deque()
        self._waiting: asyncio.Future[BaseEvent | None] | None = None
        self._closed = False

    def push(self, evt: BaseEvent) -> None:
        if self._closed:
            return
        if self._waiting is not None and not self._waiting.done():
            w, self._waiting = self._waiting, None
            w.set_result(evt)
        elif len(self._buffer) < self._limit:
            self._buffer.append(evt)
        else:
            self._client._logger.warn({"type": evt.type}, "events() consumer too slow; dropping event")

    def close(self) -> None:
        self._closed = True
        self._client._pull_queues.discard(self)
        if self._waiting is not None and not self._waiting.done():
            self._waiting.set_result(None)


class EventStream:
    """Pull-mode event stream (``async for evt in client.events()``), in dispatch order."""

    def __init__(self, queue: _PullQueue) -> None:
        self._queue = queue

    def __aiter__(self) -> EventStream:
        return self

    async def __anext__(self) -> BaseEvent:
        q = self._queue
        if q._buffer:
            return q._buffer.popleft()
        if q._closed:
            raise StopAsyncIteration
        q._waiting = asyncio.get_running_loop().create_future()
        evt = await q._waiting
        if evt is None:
            raise StopAsyncIteration
        return evt

    async def aclose(self) -> None:
        self._queue.close()

    def close(self) -> None:
        self._queue.close()


class GabrielClient:
    """High-level semantic client built entirely on :class:`GabrielCore`.

    Recommended entry point::

        async with GabrielClient.from_cookie(cookie_d=..., workspace=...) as client:
            ...
    """

    @classmethod
    def from_cookie(
        cls,
        *,
        cookie_d: str,
        token: str | None = None,
        workspace: str | None = None,
        allowed_teams: Sequence[str] | None = None,
        **options: Any,
    ) -> GabrielClient:
        """Only the session cookie is required."""
        auth = AuthOptions(
            cookie_d=cookie_d,
            token=token,
            workspace=workspace,
            allowed_teams=list(allowed_teams) if allowed_teams is not None else None,
        )
        return cls(auth=auth, **options)

    def __init__(
        self,
        *,
        auth: AuthOptions,
        concurrency: ConcurrencyOptions | None = None,
        hydration: HydrationOptions | None = None,
        loop_guard: LoopGuardOptions | None = None,
        chat: ChatSchedulerOptions | None = None,
        recovery: RecoveryOptions | None = None,
        state_store: StateStore | None = None,
        logger: Logger | None = None,
        metrics: MetricsSink | None = None,
        advanced: AdvancedOptions | None = None,
    ) -> None:
        if not isinstance(auth, AuthOptions):
            raise GabrielError("INVALID_ARGUMENT", "GabrielClient requires `auth`")
        self._logger: Logger = logger or stdlib_logger()
        self._metrics: MetricsSink = metrics or noop_metrics
        self._store: StateStore = state_store if state_store is not None else MemoryStateStore()
        adv = advanced or AdvancedOptions()
        self.core = GabrielCore(
            auth=auth,
            logger=self._logger,
            metrics=self._metrics,
            metadata=adv.metadata,
            endpoints=adv.endpoints,
            http_client=adv.http_client,
            ws_factory=adv.ws_factory,
            gateway_server=adv.gateway_server,
            ws_timings=adv.ws_timings,
        )
        self._start_timeout = adv.start_timeout
        c = concurrency or ConcurrencyOptions()
        h = hydration or HydrationOptions()
        lg = loop_guard or LoopGuardOptions()
        r = recovery or RecoveryOptions()
        self._max_inflight_events = c.max_inflight_events
        self._handler_timeout = c.handler_timeout
        self._executor_key_mode = c.executor_key
        self._hydrate_budget = h.hydrate_budget
        self._include_self_messages = lg.include_self_messages
        self._max_pages_per_channel = r.max_pages_per_channel
        self._resync_buffer_limit = r.resync_buffer_limit
        self._preload_usergroups = h.preload_usergroups

        self._executor: KeyedExecutor[_Routed] = KeyedExecutor(
            global_concurrency=c.global_concurrency, per_key_concurrency=c.per_key_concurrency, max_queue=c.max_queue
        )
        self._hydrator = Hydrator(self.core, self._logger)
        self._registry = SelfRegistry(lambda: self.core.session.user_id)
        self._loop_breaker = LoopBreaker(lg.max_auto_replies_per_minute)
        self._marks = WatermarkTracker(
            self._store, lambda: self.core.session.team_id, r.watched_threads_limit, self._logger
        )
        self._unread = UnreadCursorGuard(self.core, self._registry, self._logger)
        self._waits = WaitRegistry()

        client = self

        class _Hooks:
            def before_auto_write(self, channel: str, thread_ts: str | None) -> None:
                client._check_loop(channel, thread_ts)

            async def with_unread_guard(self, channel: str, enabled: bool, fn: Callable[[], Awaitable[T]]) -> T:
                return await client._unread.run(channel, enabled, fn)

            def note_thread_activity(self, channel: str, thread_ts: str) -> None:
                client._marks.watch_thread(channel, thread_ts)

        self.chat = ChatApi(self.core, self._registry, _Hooks(), self._logger, chat)
        self.threads = ThreadsApi(self.core, self._hydrator, self._marks.watch_thread, self._marks.unwatch_thread)
        self.files = FilesApi(self.core, self._hydrator, self._registry.register_client_msg_id)
        self.search = SearchApi(self.core, self._hydrator)
        self.reactions = ReactionsApi(self.core)
        self.users = UsersApi(self._hydrator)
        self.channels = ChannelsApi(self.core, self._hydrator)
        self.presence = PresenceApi(self.core, self._hydrator, self._emit_synthetic, self._logger)

        self._handlers: dict[str, list[Handler]] = {}
        self._middlewares: list[Middleware] = []
        self._error_handlers: list[ErrorHandler] = []
        self._pull_queues: set[_PullQueue] = set()
        self._seen: LruCache[str, bool] = LruCache(10_000, 30 * 60.0)
        self._lanes: dict[str, asyncio.Future[None]] = {}
        #: "channel:ts" → abort controllers of handlers triggered by that message.
        self._inflight_by_ts: dict[str, set[AbortController]] = {}
        self._all_controllers: set[AbortController] = set()
        self._inflight_events = 0
        #: Drafts counted in ``_inflight_events`` and not yet completed (keyed by identity).
        self._pending: dict[int, Draft] = {}
        self._hello_epoch = 0
        self._last_badge_key: str | None = None
        self._resync_buffer: list[Draft] | None = None
        self._connected_once = False
        self._stopping = False
        self._started = False
        self._event_seq = 0
        self._ready_waiters: list[asyncio.Future[None]] = []
        self._reconcile_timer: asyncio.TimerHandle | None = None
        self._tasks: set[asyncio.Task[Any]] = set()

        self.core.on_raw_frame(self._on_frame)
        self.core.on_hello(self._on_hello_info)
        self.core.ws.on("reconnecting", self._on_reconnecting)
        self.core.ws.on("state", self._on_ws_state)
        self.core.on_auth_event(self._on_auth_event)
        self.core.on_protocol_drift(self._on_drift)

    # -------------------------------------------------------------------------------------------
    # core callbacks

    def _on_hello_info(self, info: HelloInfo) -> None:
        self._on_hello(info.epoch, info.fast_reconnect)

    def _on_reconnecting(self, info: ReconnectingInfo) -> None:
        self._emit_synthetic(
            ConnectionReconnectingEvent(
                event_id=self._synthetic_id("connection.reconnecting"),
                attempt=info.attempt,
                delay_ms=round(info.delay * 1000),
            ),
            "__connection__",
        )

    def _on_ws_state(self, state: WsState, _prev: WsState) -> None:
        if state == "AUTH_DEAD":
            self._fail_ready_waiters(GabrielError("AUTH_DEAD", "root session credential is no longer valid"))

    def _on_auth_event(self, evt: AuthEvent) -> None:
        if isinstance(evt, AuthDead):
            self._emit_synthetic(
                AuthDeadEvent(event_id=self._synthetic_id("auth.dead"), team_id=evt.team_id, reason=evt.reason),
                "__auth__",
            )
            self._fail_ready_waiters(
                GabrielError("AUTH_DEAD", f"root session credential is no longer valid ({evt.reason})")
            )
            self.core.ws.stop()
        else:
            self._emit_synthetic(
                AuthRefreshedEvent(
                    event_id=self._synthetic_id("auth.refreshed"),
                    team_id=evt.team_id,
                    trigger=evt.trigger,
                    refreshed_at=evt.refreshed_at,
                ),
                "__auth__",
            )

    def _on_drift(self, info: ProtocolDriftInfo) -> None:
        self._emit_synthetic(
            ProtocolDriftEvent(
                event_id=self._synthetic_id("guard.protocol_drift"),
                unknown_type=info.unknown_type,
                unknown_subtype=info.unknown_subtype,
                sample_frame=info.sample_frame,
            ),
            "__guard__",
        )

    @property
    def self_user_id(self) -> str:
        """Current user ID (available after ``start()``)."""
        return self.core.session.user_id

    @property
    def team_id(self) -> str:
        return self.core.session.team_id

    # -------------------------------------------------------------------------------------------
    # lifecycle

    async def start(self) -> None:
        """Exchanges credentials, connects, and returns on the first ``connection.ready``."""
        if self._started:
            raise GabrielError("INVALID_STATE", "client already started")
        self._started = True
        self._stopping = False
        try:
            session = await self.core.initialize()
        except BaseException:
            self._started = False
            raise
        await self._marks.load()
        if self._preload_usergroups:
            # Wait at most 5 s, but let a slow usergroups.list finish in the background.
            preload = spawn(self._hydrator.preload_usergroups(session.user_id), self._tasks)
            await asyncio.wait({preload}, timeout=5.0)
        ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._ready_waiters.append(ready)
        await self.core.connect()
        try:
            await asyncio.wait_for(asyncio.shield(ready), self._start_timeout)
        except BaseException as err:
            try:
                await self.stop(drain=0)
            except Exception:
                pass
            if isinstance(err, TimeoutError):
                raise GabrielError("NETWORK_TIMEOUT", f"not connected within {self._start_timeout:g}s") from None
            raise

    async def stop(self, *, drain: float = 10.0) -> None:
        """Graceful shutdown.

        Stop intake → drain handlers (abort after ``drain`` seconds) → interrupt open streams →
        drain sends → persist state → close with 1000.
        """
        if self._stopping:
            return
        self._stopping = True
        if self._reconcile_timer is not None:
            self._reconcile_timer.cancel()
            self._reconcile_timer = None
        # Events still hydrating are dropped by _route() (and pinned so a later recovery refetches them).
        lanes = [asyncio.shield(f) for f in self._lanes.values()]
        if lanes:
            await asyncio.wait(lanes, timeout=max(1.0, drain))
        try:
            await asyncio.wait_for(self._executor.idle(), max(0.0, drain))
        except TimeoutError:
            for c in list(self._all_controllers):
                c.abort(cancelled_error("client stopping"))
            try:
                await asyncio.wait_for(self._executor.idle(), 1.0)
            except TimeoutError:
                pass
        try:
            await self.chat.interrupt_all()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.chat.drain(), 5.0)
        except (TimeoutError, Exception):
            pass
        try:
            await self._marks.flush()
        except Exception as err:
            self._logger.warn({"err": str(err)}, "watermark flush failed")
        try:
            await self._store.close()
        except Exception:
            pass
        self.presence.stop()
        await self.core.close()
        for q in list(self._pull_queues):
            q.close()
        self._started = False

    async def __aenter__(self) -> GabrielClient:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.stop()

    def health(self) -> HealthReport:
        ws_state = self.core.ws_state
        auth_state = self.core.auth_manager.state
        status: Literal["healthy", "degraded", "dead"] = (
            "dead" if auth_state == "AUTH_DEAD" else "healthy" if ws_state == "READY" else "degraded"
        )
        return HealthReport(
            status=status,
            ws_state=ws_state,
            auth_state=auth_state,
            last_frame_age=self.core.ws.last_frame_age,
            queue_depth=self._executor.queue_depth,
        )

    def scope(self, policy: ScopePolicy) -> ScopedClient:
        """Least-privilege view for LLM tools."""
        return ScopedClient(
            policy,
            chat=self.chat,
            threads=self.threads,
            files=self.files,
            search=self.search,
            reactions=self.reactions,
            users=self.users,
            channels=self.channels,
            presence=self.presence,
            registry=self._registry,
            logger=self._logger,
        )

    # -------------------------------------------------------------------------------------------
    # subscriptions

    @overload
    def on(self, selector: EventSelector) -> Callable[[H], H]: ...
    @overload
    def on(self, selector: EventSelector, handler: Handler) -> Callable[[], None]: ...
    def on(self, selector: EventSelector, handler: Handler | None = None) -> Callable[[], None] | Callable[[H], H]:
        """Registers a handler for an event type (string or event class).

        ``client.on(MessageCreatedEvent, handler)`` returns an unsubscribe callable; used as a
        decorator (``@client.on("auth.dead")``) it returns the function unchanged. Handlers may
        be sync or async and receive ``(evt, ctx)``.
        """
        type_name = event_type_of(selector)
        if handler is None:

            def decorator(fn: H) -> H:
                self._add_handler(type_name, fn)
                return fn

            return decorator
        return self._add_handler(type_name, handler)

    def _add_handler(self, type_name: str, handler: Handler) -> Callable[[], None]:
        handlers = self._handlers.setdefault(type_name, [])
        if handler not in handlers:  # Set semantics, like the reference
            handlers.append(handler)

        def off() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return off

    def off(self, selector: EventSelector, handler: Handler) -> None:
        handlers = self._handlers.get(event_type_of(selector), [])
        if handler in handlers:
            handlers.remove(handler)

    @overload
    def on_message(
        self,
        matcher: MessageMatcher | MessagePredicate | None = None,
        handler: None = None,
        *,
        channel: str | Sequence[str] | None = None,
        mentions_me: bool | None = None,
        pattern: Any = None,
    ) -> Callable[[H], H]: ...
    @overload
    def on_message(
        self,
        matcher: MessageMatcher | MessagePredicate | None,
        handler: Handler,
        *,
        channel: str | Sequence[str] | None = None,
        mentions_me: bool | None = None,
        pattern: Any = None,
    ) -> Callable[[], None]: ...
    def on_message(
        self,
        matcher: MessageMatcher | MessagePredicate | None = None,
        handler: Handler | None = None,
        *,
        channel: str | Sequence[str] | None = None,
        mentions_me: bool | None = None,
        pattern: Any = None,
    ) -> Callable[[], None] | Callable[[H], H]:
        """Declarative ``message.created`` routing.

        ``client.on_message(MessageMatcher(mentions_me=True), handler)`` or as a decorator:
        ``@client.on_message(mentions_me=True)``.
        """
        if matcher is None:
            matcher = MessageMatcher(channel=channel, mentions_me=mentions_me, pattern=pattern)
        predicate = matcher_to_predicate(matcher)

        def register(fn: Handler) -> Callable[[], None]:
            async def filtered(evt: MessageCreatedEvent, ctx: EventContextImpl) -> None:
                if predicate(evt):
                    await _maybe_await(fn(evt, ctx))

            return self._add_handler("message.created", filtered)

        if handler is None:

            def decorator(fn: H) -> H:
                register(fn)
                return fn

            return decorator
        return register(handler)

    @overload
    def on_thread_reply(self, handler: None = None) -> Callable[[H], H]: ...
    @overload
    def on_thread_reply(self, handler: Handler) -> Callable[[], None]: ...
    def on_thread_reply(self, handler: Handler | None = None) -> Any:
        """Thread replies only (filter over ``message.created``). Usable as a decorator."""

        def register(fn: Handler) -> Callable[[], None]:
            async def filtered(evt: MessageCreatedEvent, ctx: EventContextImpl) -> None:
                if evt.is_thread_reply:
                    await _maybe_await(fn(evt, ctx))

            return self._add_handler("message.created", filtered)

        if handler is None:

            def decorator(fn: H) -> H:
                register(fn)
                return fn

            return decorator
        return register(handler)

    def on_error(self, handler: ErrorHandler) -> Callable[[], None]:
        if handler not in self._error_handlers:
            self._error_handlers.append(handler)

        def off() -> None:
            if handler in self._error_handlers:
                self._error_handlers.remove(handler)

        return off

    def use(self, mw: Middleware) -> None:
        """Onion middleware around every handler invocation: ``async def mw(evt, ctx, next)``."""
        self._middlewares.append(mw)

    def events(self, *, buffer_size: int = 10_000) -> EventStream:
        """Pull-mode event stream (in dispatch order)."""
        q = _PullQueue(self, buffer_size)
        self._pull_queues.add(q)
        return EventStream(q)

    # -------------------------------------------------------------------------------------------
    # inbound pipeline

    def _on_frame(self, frame: RawWsFrame) -> None:
        if self._stopping:
            return
        t = frame.get("type")
        if not isinstance(t, str) or t in _CONTROL_TYPES:
            return
        if isinstance(frame.get("reply_to"), int | float) and not isinstance(frame.get("reply_to"), bool):
            return
        self._metrics.increment("events_received_total", {"type": t})
        if self.presence.handle_frame(frame):
            return

        if t == "subteam_self_added":
            if is_subteam_id(frame.get("subteam_id")):
                self._hydrator.my_subteams.add(frame["subteam_id"])
            return
        if t == "subteam_self_removed":
            if is_subteam_id(frame.get("subteam_id")):
                self._hydrator.my_subteams.discard(frame["subteam_id"])
            return
        if t == "subteam_members_changed":
            self._on_subteam_members_changed(frame)
            return
        if t in ("subteam_updated", "subteam_created"):
            raw_st = frame.get("subteam")
            st: dict[str, Any] = raw_st if isinstance(raw_st, dict) else {}
            if st.get("id"):
                users = st.get("users")
                self._hydrator.upsert_usergroup(
                    UsergroupRecord(
                        id=st["id"],
                        handle=st.get("handle") or "",
                        name=st.get("name") or None,
                        users=list(users) if isinstance(users, list) else None,
                    )
                )
        elif t in ("channel_rename", "group_rename", "im_created", "channel_archive", "group_archive"):
            ch = frame.get("channel")
            id = ch if isinstance(ch, str) else ch.get("id") if isinstance(ch, dict) else None
            if id:
                self._hydrator.invalidate_channel(id)
        elif t in ("channel_marked", "im_marked", "group_marked", "mpim_marked"):
            channel, ts = frame.get("channel"), frame.get("ts")
            if isinstance(channel, str) and isinstance(ts, str):
                self._unread.on_marked(channel, ts)
                if self._registry.is_self_mark(channel, ts):
                    return  # SDK-caused echo
        elif t == "message":
            self._on_message_frame(frame)

        draft = normalize_frame(frame, self._normalize_context("realtime"))
        if draft is None:
            return
        evt = draft.event
        if isinstance(evt, MessageOtherEvent) and evt.raw_subtype not in KNOWN_MESSAGE_SUBTYPES:
            self.core.report_drift("message", evt.raw_subtype, frame)
        if isinstance(evt, UnreadBadgeCountsEvent):
            key = badge_snapshot_key(evt)
            if key == self._last_badge_key:
                return
            self._last_badge_key = key
        self._dispatch(draft, "realtime")

    def _on_message_frame(self, frame: RawWsFrame) -> None:
        channel = frame.get("channel") if isinstance(frame.get("channel"), str) else None
        subtype = frame.get("subtype") if isinstance(frame.get("subtype"), str) else None
        if subtype == "message_deleted" and channel:
            # Control-plane bypass: cancel in-flight work for the deleted message immediately.
            deleted_ts = frame.get("deleted_ts") if isinstance(frame.get("deleted_ts"), str) else frame.get("ts")
            if isinstance(deleted_ts, str):
                for c in list(self._inflight_by_ts.get(f"{channel}:{deleted_ts}", ())):
                    c.abort(cancelled_error("message_deleted"))
                self.chat.note_deleted(deleted_ts)
            return
        msg = frame.get("message") if subtype == "message_changed" else frame
        if not isinstance(msg, dict):
            msg = {}
        ts = msg.get("ts") if isinstance(msg.get("ts"), str) else None
        if ts:
            thread_ts = msg.get("thread_ts")
            self._hydrator.record_thread_root(ts, thread_ts if isinstance(thread_ts, str) else None)
        cmid = msg.get("client_msg_id")
        if isinstance(cmid, str) and ts and msg.get("user") == self.core.session.user_id:
            self.chat.note_echo(cmid, ts)

    def _on_subteam_members_changed(self, frame: RawWsFrame) -> None:
        id = frame.get("subteam_id") if isinstance(frame.get("subteam_id"), str) else None
        if not id:
            return
        added = [u for u in frame.get("added_users") or [] if isinstance(u, str)]
        removed = {u for u in frame.get("removed_users") or [] if isinstance(u, str)}
        me = self.core.session.user_id
        if me in added:
            self._hydrator.my_subteams.add(id)
        if me in removed:
            self._hydrator.my_subteams.discard(id)
        rec = self._hydrator.usergroup(id)
        if rec is None or rec.users is None:
            return
        users = [u for u in rec.users if u not in removed] + [u for u in added if u not in rec.users]
        self._hydrator.upsert_usergroup(UsergroupRecord(id=rec.id, handle=rec.handle, name=rec.name, users=users))
        event_ts = frame.get("event_ts")
        suffix = str(event_ts) if event_ts is not None else self._synthetic_id("subteam")
        self._emit_synthetic(
            UserSubteamUpdatedEvent(
                event_id=f"subteam:{id}:{suffix}", subteam_id=id, handle=rec.handle, member_ids=users
            ),
            id,
        )

    def _normalize_context(self, delivery: Literal["realtime", "backfill"]) -> NormalizeContext:
        registry = self._registry

        def classify(info: OriginInfo) -> Any:
            return registry.classify(
                ts=info.ts,
                content_hash=info.content_hash,
                is_edit=info.is_edit,
                user=info.user,
                bot_id=info.bot_id,
                client_msg_id=info.client_msg_id,
            )

        return NormalizeContext(
            self_user_id=self.core.session.user_id,
            classify_origin=classify,
            is_my_subteam=self._hydrator.my_subteams.__contains__,
            delivery=delivery,
        )

    def _executor_key_of(self, channel_id: str | None, lane_key: str, thread_key: str | None) -> str:
        if self._executor_key_mode == "thread" and channel_id and thread_key:
            return f"{channel_id}:{thread_key}"
        return channel_id or lane_key

    def _executor_key_of_draft(self, draft: Draft) -> str:
        return self._executor_key_of(draft.channel_id, draft.lane_key, draft.thread_key)

    def _chain(self, lane: str, step: Callable[[], Awaitable[None]]) -> None:
        """Runs ``step`` after everything already queued on ``lane`` (per-lane dispatch order)."""
        prev = self._lanes.get(lane)

        async def run() -> None:
            if prev is not None:
                await asyncio.wait([prev])
            await step()

        nxt = spawn(run(), self._tasks)
        self._lanes[lane] = nxt

        def cleanup(_: object) -> None:
            if self._lanes.get(lane) is nxt:
                del self._lanes[lane]

        nxt.add_done_callback(cleanup)

    def _dispatch(self, draft: Draft, delivery: Literal["realtime", "backfill"]) -> None:
        if self._resync_buffer is not None and delivery == "realtime":
            if len(self._resync_buffer) >= self._resync_buffer_limit:
                self._overflow(draft)
                return
            self._resync_buffer.append(draft)
            return
        event_id = draft.event.event_id
        if draft.dedupe and self._seen.has(event_id):
            return
        if self._inflight_events >= self._max_inflight_events:
            self._overflow(draft)
            return
        if draft.dedupe:
            self._seen.set(event_id, True)
        self._begin(draft)
        thread_ts = self._thread_of_draft(draft)
        if draft.created_ts and draft.channel_id:
            self._marks.begin(draft.channel_id, draft.created_ts, thread_ts)
        hydration = asyncio.ensure_future(self._hydrator.hydrate(draft, self._hydrate_budget))

        async def step() -> None:
            try:
                h = await hydration
                self._route(draft, h)
            except Exception as err:
                self._logger.error({"err": str(err), "type": draft.event.type}, "dispatch failed")
                self._complete(draft)

        self._chain(draft.lane_key, step)

    def _thread_of_draft(self, draft: Draft) -> str | None:
        thread = getattr(draft.event, "thread", None)
        return getattr(thread, "root_ts", None)

    def _begin(self, draft: Draft) -> None:
        self._inflight_events += 1
        self._pending[id(draft)] = draft

    def _forget(self, draft: Draft) -> bool:
        """Removes a draft from the in-flight count; False when it was already completed."""
        if self._pending.pop(id(draft), None) is None:
            return False
        self._inflight_events = max(0, self._inflight_events - 1)
        return True

    def _overflow(self, draft: Draft) -> None:
        self._metrics.increment("events_dropped_total", {"type": draft.event.type})
        if draft.created_ts and draft.channel_id:
            self._marks.pin(draft.channel_id, draft.created_ts, self._thread_of_draft(draft))
        self._seen.delete(draft.event.event_id)
        self._emit_guard(
            OverflowEvent(
                event_id=self._synthetic_id("guard.overflow"), channel_id=draft.channel_id, ts=draft.created_ts
            )
        )

    def _complete(self, draft: Draft) -> None:
        if not self._forget(draft):
            return
        if draft.created_ts and draft.channel_id:
            self._marks.complete(draft.channel_id, draft.created_ts, self._thread_of_draft(draft))

    def _route(self, draft: Draft, hydration: HydrationResult) -> None:
        evt = draft.event
        if self._stopping and not evt.type.startswith("auth.") and not evt.type.startswith("connection."):
            if draft.created_ts and draft.channel_id:
                self._marks.pin(draft.channel_id, draft.created_ts, self._thread_of_draft(draft))
            self._forget(draft)
            return
        self._metrics.observe(
            "hydration_latency_ms", 0 if hydration.complete else self._hydrate_budget * 1000, {"type": evt.type}
        )
        if (
            isinstance(evt, MessageCreatedEvent | MessageUpdatedEvent)
            and evt.origin == "this_client"
            and not self._include_self_messages
        ):
            self._complete(draft)
            return
        if self._waits.route(evt):
            self._complete(draft)
            return
        for q in list(self._pull_queues):
            q.push(evt)
        handlers = self._handlers.get(evt.type)
        if not handlers:
            self._complete(draft)
            return
        snapshot = list(handlers)

        async def job(handle: JobHandle) -> None:
            try:
                await self._run_handlers(snapshot, draft, hydration, handle)
            finally:
                self._complete(draft)

        submitted = self._executor.submit(self._executor_key_of_draft(draft), _Routed(draft, hydration), job)
        if not submitted:
            self._forget(draft)
            self._overflow(draft)

    def _emit_synthetic(self, evt: BaseEvent, lane_key: str) -> None:
        """Synthetic (client-generated) events go through the same lanes, without hydration."""
        if self._stopping and evt.type != "auth.dead":
            return
        draft = Draft(event=evt, lane_key=lane_key, needs=EntityNeeds(), dedupe=False)  # type: ignore[arg-type]
        self._begin(draft)

        async def step() -> None:
            self._route(draft, _COMPLETE_HYDRATION)

        self._chain(lane_key, step)

    def _emit_guard(self, evt: BaseEvent) -> None:
        # Guard events bypass bounds so overflow can always be observed.
        for q in list(self._pull_queues):
            q.push(evt)
        for h in list(self._handlers.get(evt.type, ())):
            spawn(self._run_detached(h, evt), self._tasks)

    async def _run_detached(self, handler: Handler, evt: BaseEvent) -> None:
        try:
            await _maybe_await(handler(evt, self._detached_context(evt)))
        except Exception as err:
            self._report_error(err, evt)

    def _synthetic_id(self, kind: str) -> str:
        self._event_seq += 1
        return f"{kind}:{int(time.time() * 1000)}:{self._event_seq}"

    async def _run_handlers(
        self, handlers: list[Handler], draft: Draft, hydration: HydrationResult, job: JobHandle
    ) -> None:
        for handler in handlers:
            if self._stopping and not self._all_controllers and draft.event.type.startswith("connection."):
                break
            await self._run_one(handler, draft, hydration, job)

    async def _run_one(self, handler: Handler, draft: Draft, hydration: HydrationResult, job: JobHandle) -> None:
        evt = draft.event
        controller = AbortController()
        self._all_controllers.add(controller)
        trigger = trigger_of(evt)
        ts_key = f"{trigger.channel}:{trigger.ts}" if trigger.channel and trigger.ts else None
        if ts_key:
            self._inflight_by_ts.setdefault(ts_key, set()).add(controller)
        loop = asyncio.get_running_loop()
        writes_cancelled = False
        remaining = self._handler_timeout
        timer_started_at = loop.time()
        timer: asyncio.TimerHandle | None = None
        grace_timer: asyncio.TimerHandle | None = None

        def on_grace() -> None:
            nonlocal writes_cancelled
            writes_cancelled = True
            job.force_release()
            self._complete(draft)  # a timed-out event counts as handled (failed); watermarks advance

        def on_timeout() -> None:
            nonlocal grace_timer
            err = GabrielError("HANDLER_TIMEOUT", f"handler exceeded {self._handler_timeout:g}s")
            controller.abort(err)
            self._report_error(err, evt)
            grace_timer = loop.call_later(5.0, on_grace)

        def arm_timer() -> None:
            nonlocal timer, timer_started_at
            if self._handler_timeout <= 0:
                return
            timer_started_at = loop.time()
            timer = loop.call_later(max(0.0, remaining), on_timeout)

        def pause_timer() -> None:
            nonlocal timer, remaining
            if timer is None:
                return
            timer.cancel()
            timer = None
            remaining -= loop.time() - timer_started_at

        def suspend() -> None:
            pause_timer()
            job.suspend()

        async def resume() -> None:
            await job.resume()
            if not controller.signal.aborted:
                arm_timer()

        ctx = EventContextImpl(self, evt, controller, hydration, lambda: writes_cancelled, _JobControl(suspend, resume))
        arm_timer()
        token = set_trigger(trigger)
        try:
            await self._compose(handler)(evt, ctx)
        except Exception as err:
            if not (isinstance(err, GabrielError) and err.code == "CANCELLED" and controller.signal.aborted):
                self._report_error(err, evt)
        finally:
            reset_trigger(token)
            if timer is not None:
                timer.cancel()
            if grace_timer is not None:
                grace_timer.cancel()
            self._all_controllers.discard(controller)
            if ts_key:
                controllers = self._inflight_by_ts.get(ts_key)
                if controllers is not None:
                    controllers.discard(controller)
                    if not controllers:
                        del self._inflight_by_ts[ts_key]

    def _compose(self, handler: Handler) -> Callable[[BaseEvent, EventContextImpl], Awaitable[None]]:
        mws = list(self._middlewares)

        async def run(evt: BaseEvent, ctx: EventContextImpl) -> None:
            index = -1

            async def dispatch(i: int) -> None:
                nonlocal index
                if i <= index:
                    raise GabrielError("INVALID_STATE", "next() called multiple times")
                index = i
                if i == len(mws):
                    await _maybe_await(handler(evt, ctx))
                    return
                await mws[i](evt, ctx, lambda: dispatch(i + 1))

            await dispatch(0)

        return run

    def _report_error(self, err: BaseException, evt: BaseEvent | None = None) -> None:
        if not self._error_handlers:
            self._logger.error(
                {
                    "err": f"{type(err).__name__}: {err}",
                    "event_id": evt.event_id if evt is not None else None,
                    "type": evt.type if evt is not None else None,
                },
                "handler error",
            )
            return
        for h in list(self._error_handlers):
            try:
                h(err, evt)
            except Exception as inner:
                self._logger.error({"err": str(inner)}, "on_error handler threw")

    def _detached_context(self, evt: BaseEvent) -> EventContextImpl:
        return EventContextImpl(self, evt, AbortController(), _COMPLETE_HYDRATION, lambda: False, _NOOP_JOB)

    async def _wait_for_reply(
        self,
        trigger_evt: BaseEvent,
        flt: MessageMatcher | MessagePredicate,
        timeout: float,
        consume: bool,
        exclusive: bool,
        signal: AbortSignal,
        job: _JobControl,
    ) -> MessageCreatedEvent | None:
        if not isinstance(timeout, int | float) or timeout <= 0:
            raise GabrielError("INVALID_ARGUMENT", "wait_for_reply: timeout must be a positive number of seconds")
        predicate = matcher_to_predicate(flt)
        after_ts = getattr(trigger_evt, "ts", None)
        after_ts = after_ts if isinstance(after_ts, str) else None
        # 1. Already queued for this executor key?
        t = trigger_of(trigger_evt)
        key = self._executor_key_of(t.channel, t.channel or "", t.thread_root_ts)

        def matches(m: _Routed) -> bool:
            e = m.draft.event
            return (
                isinstance(e, MessageCreatedEvent)
                and (not after_ts or compare_ts(e.ts, after_ts) > 0)
                and _safe(predicate, e)
            )

        found, pending = self._executor.extract_pending(key, matches)
        if found and pending is not None:
            if not consume:
                # Not consuming: leave normal handling intact by re-submitting.
                handlers = self._handlers.get("message.created")
                if handlers:
                    snapshot = list(handlers)
                    routed = pending

                    async def rerun(j: JobHandle) -> None:
                        try:
                            await self._run_handlers(snapshot, routed.draft, routed.hydration, j)
                        finally:
                            self._complete(routed.draft)

                    self._executor.submit(key, pending, rerun)
                else:
                    self._complete(pending.draft)
            else:
                self._complete(pending.draft)
            assert isinstance(pending.draft.event, MessageCreatedEvent)
            return pending.draft.event
        # 2. Recently routed but unconsumed?
        recent = self._waits.take_recent(predicate, after_ts, consume)
        if recent is not None:
            if consume:
                q_found, queued = self._executor.extract_pending(
                    key, lambda m: m.draft.event.event_id == recent.event_id
                )
                if q_found and queued is not None:
                    self._complete(queued.draft)
            return recent
        # 3. Register and wait without holding executor permits.
        signal.throw_if_aborted()
        job.suspend()
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[MessageCreatedEvent | None] = loop.create_future()

        def on_abort() -> None:
            if not fut.done():
                fut.set_exception(cancelled_error(signal.reason))

        def on_match(e: MessageCreatedEvent) -> None:
            if not fut.done():
                fut.set_result(e)

        def on_timer() -> None:
            if not fut.done():
                fut.set_result(None)

        remove_abort = signal.add_listener(on_abort)
        timer = loop.call_later(timeout, on_timer)
        remove = self._waits.add(
            Waiter(predicate=predicate, consume=consume, exclusive=exclusive, resolve=on_match, after_ts=after_ts)
        )
        try:
            return await fut
        finally:
            timer.cancel()
            remove_abort()
            remove()
            await job.resume()

    def _check_loop(self, channel: str, thread_ts: str | None) -> None:
        if current_trigger() is None:
            return  # only automatic (handler-driven) writes count
        check = self._loop_breaker.try_record(channel, thread_ts)
        if check.allowed:
            return
        self._emit_guard(
            LoopDetectedEvent(
                event_id=self._synthetic_id("guard.loop_detected"),
                channel_id=channel,
                thread_ts=thread_ts or None,
                reply_count_in_window=check.count,
            )
        )
        where = f"{channel}/{thread_ts}" if thread_ts else channel
        raise GabrielError("LOOP_DETECTED", f"auto-reply circuit open for {where} ({check.count} writes in 60s)")

    # -------------------------------------------------------------------------------------------
    # connection lifecycle

    def _on_hello(self, epoch: int, fast_reconnect: bool) -> None:
        # Runs synchronously inside the frame reader (like Node) so frames queued right behind
        # `hello` already see the resync buffer; only the recovery itself is asynchronous.
        self._hello_epoch = epoch
        self.presence.on_hello(epoch)
        had_connection = self._connected_once
        self._connected_once = True
        needs_cold_resync = not fast_reconnect and (
            had_connection or self._store.persistent or self._marks.has_pinned()
        )
        if fast_reconnect or not needs_cold_resync:
            self._flush_resync_buffer()  # a resync interrupted by this reconnect must not strand its buffer
            self.core.ws.mark_ready(False, epoch)
            self._emit_ready(fast_reconnect, False)
            if fast_reconnect:
                if self._reconcile_timer is not None:
                    self._reconcile_timer.cancel()

                def reconcile() -> None:
                    self._reconcile_timer = None
                    spawn(self._run_recovery("fast_reconnect_reconcile", False), self._tasks)

                self._reconcile_timer = asyncio.get_running_loop().call_later(2.0, reconcile)
            return
        # Keep (merge into) the buffer of a resync this reconnect interrupted.
        if self._resync_buffer is None:
            self._resync_buffer = []
        cause: Literal["overflow", "cold_resync"] = (
            "overflow" if self._marks.has_pinned() and had_connection else "cold_resync"
        )
        spawn(self._cold_resync(epoch, cause), self._tasks)

    async def _cold_resync(self, epoch: int, cause: Literal["overflow", "cold_resync"]) -> None:
        degraded = False
        try:
            degraded = not await self._run_recovery(cause, True)
        finally:
            # A newer hello owns the buffer and the READY transition.
            if self._hello_epoch == epoch:
                self._flush_resync_buffer()
        if self._hello_epoch != epoch:
            return
        self.core.ws.mark_ready(degraded, epoch)
        self._emit_ready(False, degraded)

    def _flush_resync_buffer(self) -> None:
        buffered = self._resync_buffer or []
        self._resync_buffer = None
        for d in buffered:
            self._dispatch(d, "realtime")

    async def _run_recovery(
        self, cause: Literal["fast_reconnect_reconcile", "cold_resync", "overflow"], blocking: bool
    ) -> bool:
        """Returns True when the gap was fully recovered."""
        try:
            result = await recover_gap(
                self.core,
                self.channels,
                self._marks,
                max_pages_per_channel=self._max_pages_per_channel,
                logger=self._logger,
            )
            first_buffered = next((d.created_ts for d in self._resync_buffer or [] if d.created_ts), None)
            late: list[Draft] = []
            for frame in result.frames:
                draft = normalize_frame(frame, self._normalize_context("backfill"))
                if draft is None:
                    continue
                if (
                    blocking
                    and first_buffered
                    and draft.created_ts
                    and compare_ts(draft.created_ts, first_buffered) >= 0
                ):
                    late.append(draft)
                else:
                    self._dispatch(draft, "backfill")
            if late and self._resync_buffer is not None:
                self._resync_buffer.extend(late)
            gap = result.gap
            for ch in gap.channels_recovered:
                self._marks.unpin_all(ch)
            if gap.recovered_count > 0 or not gap.recovered:
                self._emit_synthetic(
                    ConnectionGapEvent(
                        event_id=self._synthetic_id("connection.gap"),
                        cause=cause,
                        recovered=gap.recovered,
                        recovered_count=gap.recovered_count,
                        channels_recovered=gap.channels_recovered,
                        threads_recovered=gap.threads_recovered,
                        truncated=gap.truncated,
                        unrecovered=gap.unrecovered,
                        from_ts=gap.from_ts,
                        to_ts=gap.to_ts,
                    ),
                    "__connection__",
                )
            return gap.recovered
        except Exception as err:
            self._logger.warn({"err": str(err), "cause": cause}, "gap recovery failed")
            return False

    def _emit_ready(self, fast_reconnect: bool, degraded: bool) -> None:
        self._emit_synthetic(
            ConnectionReadyEvent(
                event_id=self._synthetic_id("connection.ready"), fast_reconnect=fast_reconnect, degraded=degraded
            ),
            "__connection__",
        )
        waiters, self._ready_waiters = self._ready_waiters, []
        for w in waiters:
            if not w.done():
                w.set_result(None)

    def _fail_ready_waiters(self, err: BaseException) -> None:
        waiters, self._ready_waiters = self._ready_waiters, []
        for w in waiters:
            if not w.done():
                w.set_exception(err)


def _safe(predicate: MessagePredicate, evt: MessageCreatedEvent) -> bool:
    try:
        return bool(predicate(evt))
    except Exception:
        return False


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


__all__ = [
    "AdvancedOptions",
    "ConcurrencyOptions",
    "ErrorHandler",
    "EventContextImpl",
    "EventStream",
    "GabrielClient",
    "Handler",
    "HealthReport",
    "HydrationOptions",
    "LoopGuardOptions",
    "Middleware",
    "RecoveryOptions",
]
