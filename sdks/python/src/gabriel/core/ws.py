"""WebSocket lifecycle state machine (spec/behavior.md §5).

Node's event-callback socket becomes one reader task per connection epoch that loops on
``recv()``. Timers (hello timeout, ping, dead-peer watchdog, backoff) are ``loop.call_later``
handles, so the state machine itself stays synchronous like the reference implementation.
"""

from __future__ import annotations

import asyncio
import json
import random as _random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, overload
from urllib.parse import urlencode

from ..errors import GabrielError
from ..util.aio import full_jitter_delay, spawn
from ..util.logger import Logger, silent_logger
from ..util.redact import scrub_secrets
from .auth import AuthManager, RefreshTrigger
from .metadata import ClientMetadata, Endpoints, build_start_args
from .types import MetricsSink, RawWsFrame, WebSocketFactory, WebSocketLike, noop_metrics

FIRST_SEQ_ID = 16385  # 0x4001, as used by the web client
PING_INTERVAL = 10.0
DEAD_PEER_TIMEOUT = 25.0
HELLO_TIMEOUT = 10.0
FAST_RECONNECT_TTL = 285.0
DEFAULT_WS_COMMAND_TIMEOUT = 10.0
WATCHDOG_INTERVAL = 5.0
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
SERVER_BUSY_DEFAULT = 30.0
#: Matches the ``ws`` package default ``maxPayload`` (100 MiB); websockets defaults to 1 MiB.
MAX_FRAME_SIZE = 100 * 1024 * 1024
HANDSHAKE_TIMEOUT = 15.0

WsState = Literal[
    "DISCONNECTED",
    "CONNECTING_FAST",
    "CONNECTING_COLD",
    "AWAITING_HELLO",
    "RESYNCING",
    "READY",
    "READY_DEGRADED",
    "BACKOFF",
    "CREDENTIAL_REFRESH",
    "AUTH_DEAD",
    "STOPPED",
]

WsEventName = Literal["frame", "state", "hello", "reconnecting", "closed"]


@dataclass(frozen=True, slots=True)
class HelloInfo:
    epoch: int
    fast_reconnect: bool
    frame: RawWsFrame


@dataclass(frozen=True, slots=True)
class ReconnectingInfo:
    attempt: int
    #: Delay before the next attempt, in seconds (Node: ``delayMs``).
    delay: float


@dataclass(frozen=True, slots=True)
class ClosedInfo:
    epoch: int
    code: int


@dataclass(slots=True, kw_only=True)
class WsTimings:
    """Protocol timings in seconds. Defaults follow the spec; override only in tests."""

    hello_timeout: float = HELLO_TIMEOUT
    ping_interval: float = PING_INTERVAL
    dead_peer_timeout: float = DEAD_PEER_TIMEOUT
    watchdog_interval: float = WATCHDOG_INTERVAL
    fast_reconnect_ttl: float = FAST_RECONNECT_TTL
    command_timeout: float = DEFAULT_WS_COMMAND_TIMEOUT
    backoff_base: float = BACKOFF_BASE
    backoff_cap: float = BACKOFF_CAP
    server_busy_default: float = SERVER_BUSY_DEFAULT


class WebSocketHandshakeError(Exception):
    """An HTTP rejection of the WebSocket upgrade (Node's ``unexpected-response``).

    Custom :data:`WebSocketFactory` implementations raise this (or any exception carrying
    ``status_code`` / ``headers``, or a ``response`` with them) so the state machine can tell
    auth failures (401/403) and throttling (429 + ``Retry-After``) from network errors.
    """

    def __init__(self, status_code: int, headers: Mapping[str, str] | None = None) -> None:
        super().__init__(f"WebSocket handshake rejected (HTTP {status_code})")
        self.status_code = status_code
        self.headers: Mapping[str, str] = dict(headers or {})


def _handshake_rejection(err: BaseException) -> tuple[int, Mapping[str, Any]] | None:
    status = getattr(err, "status_code", None)
    headers = getattr(err, "headers", None)
    if not isinstance(status, int):
        response = getattr(err, "response", None)
        status = getattr(response, "status_code", None)
        headers = getattr(response, "headers", None)
    if isinstance(status, int) and not isinstance(status, bool):
        return status, headers if headers is not None else {}
    return None


def _header(headers: Mapping[str, Any], name: str) -> Any:
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.title())
        return value
    except Exception:
        return None


def _close_code(ws: WebSocketLike, err: BaseException) -> int:
    code = getattr(ws, "close_code", None)
    if isinstance(code, int):
        return code
    rcvd = getattr(err, "rcvd", None)
    code = getattr(rcvd, "code", None)
    if isinstance(code, int):
        return code
    code = getattr(err, "code", None)
    return code if isinstance(code, int) else 1006


def _is_connection_closed(err: BaseException) -> bool:
    try:
        from websockets.exceptions import ConnectionClosed
    except ImportError:  # pragma: no cover
        return False
    return isinstance(err, ConnectionClosed) or type(err).__name__ in ("ConnectionClosed", "FakeConnectionClosed")


async def default_ws_factory(url: str, headers: dict[str, str]) -> WebSocketLike:
    """Opens a connection with the ``websockets`` package.

    The browser User-Agent replaces websockets' own ``User-Agent`` header; compression and
    protocol-level pings are disabled (the Slack protocol has its own ``ping`` frames), like
    the reference implementation's ``ws`` options.
    """
    from websockets.asyncio.client import connect

    user_agent = next((v for k, v in headers.items() if k.lower() == "user-agent"), None)
    extra = {k: v for k, v in headers.items() if k.lower() != "user-agent"}
    conn = await connect(
        url,
        additional_headers=extra,
        user_agent_header=user_agent,
        compression=None,
        max_size=MAX_FRAME_SIZE,
        ping_interval=None,
        open_timeout=HANDSHAKE_TIMEOUT,
    )
    return conn


class _Pending:
    __slots__ = ("epoch", "future", "timer")

    def __init__(self, future: asyncio.Future[RawWsFrame], timer: asyncio.TimerHandle, epoch: int) -> None:
        self.future = future
        self.timer = timer
        self.epoch = epoch


_FRT_INVALID_MESSAGES = frozenset({"Subscription failure from the CS", "Socket URL has expired"})
_SERVER_BUSY_MESSAGES = frozenset({"rate_limited", "ratelimited", "Ratelimited", "backend_overloaded", "unknown"})

_CONNECTED_STATES = frozenset({"RESYNCING", "READY", "READY_DEGRADED"})


class _Interval:
    """``setInterval`` on top of ``loop.call_later``."""

    def __init__(self, interval: float, fn: Callable[[], None]) -> None:
        self._interval = interval
        self._fn = fn
        self._handle: asyncio.TimerHandle | None = None
        self._cancelled = False
        self._schedule()

    def _schedule(self) -> None:
        self._handle = asyncio.get_running_loop().call_later(self._interval, self._tick)

    def _tick(self) -> None:
        if self._cancelled:
            return
        self._schedule()
        self._fn()

    def cancel(self) -> None:
        self._cancelled = True
        if self._handle is not None:
            self._handle.cancel()


class WsConnection:
    """WebSocket lifecycle state machine (spec/behavior.md §5.2).

    Connection epochs, 10 s ping, 25 s dead-peer detection, 285 s fast-reconnect tickets,
    ``goodbye`` (4104) hand-over, full-jitter backoff and credential refresh.
    """

    def __init__(
        self,
        *,
        auth: AuthManager,
        metadata: ClientMetadata,
        endpoints: Endpoints,
        ws_factory: WebSocketFactory,
        logger: Logger = silent_logger,
        metrics: MetricsSink = noop_metrics,
        gateway_server: str | None = None,
        now: Callable[[], float] = time.monotonic,
        random: Callable[[], float] = _random.random,
        timings: WsTimings | None = None,
    ) -> None:
        self._auth = auth
        self._metadata = metadata
        self._endpoints = endpoints
        self._factory = ws_factory
        self._logger = logger
        self._metrics = metrics
        #: Gateway shard; defaults to ``{team_id}-1``.
        self._gateway_server = gateway_server
        self._now = now
        self._random = random
        self.timings = timings or WsTimings()

        self._ws: WebSocketLike | None = None
        self._run_task: asyncio.Task[Any] | None = None
        self._state: WsState = "DISCONNECTED"
        self._epoch = 0
        self._seq = FIRST_SEQ_ID
        self._pending: dict[int, _Pending] = {}
        self._frt_url: str | None = None
        self._frt_received_at = 0.0
        self._frt_token: str | None = None
        self._fast_failed = False
        self._attempt = 0
        self._last_frame_at: float | None = None
        self._stopped = True
        self._hello_timer: asyncio.TimerHandle | None = None
        self._ping_timer: _Interval | None = None
        self._watchdog_timer: _Interval | None = None
        self._backoff_timer: asyncio.TimerHandle | None = None
        self._next_delay_override: float | None = None
        self._connecting_fast = False
        self._send_lock: asyncio.Lock | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._close_tasks: set[asyncio.Task[Any]] = set()
        self._listeners: dict[str, list[Callable[..., None]]] = {
            "frame": [],
            "state": [],
            "hello": [],
            "reconnecting": [],
            "closed": [],
        }

    @property
    def state(self) -> WsState:
        return self._state

    @property
    def current_epoch(self) -> int:
        return self._epoch

    @property
    def last_frame_age(self) -> float:
        """Seconds since the last downstream frame, or ``-1.0`` before the first one (Node: ``lastFrameAgeMs``)."""
        return -1.0 if self._last_frame_at is None else self._now() - self._last_frame_at

    @overload
    def on(self, event: Literal["frame"], listener: Callable[[RawWsFrame, int], None]) -> Callable[[], None]: ...
    @overload
    def on(self, event: Literal["state"], listener: Callable[[WsState, WsState], None]) -> Callable[[], None]: ...
    @overload
    def on(self, event: Literal["hello"], listener: Callable[[HelloInfo], None]) -> Callable[[], None]: ...
    @overload
    def on(
        self, event: Literal["reconnecting"], listener: Callable[[ReconnectingInfo], None]
    ) -> Callable[[], None]: ...
    @overload
    def on(self, event: Literal["closed"], listener: Callable[[ClosedInfo], None]) -> Callable[[], None]: ...
    def on(self, event: WsEventName, listener: Callable[..., None]) -> Callable[[], None]:
        """Registers a synchronous listener; returns an unsubscribe callable."""
        listeners = self._listeners[event]
        listeners.append(listener)

        def remove() -> None:
            if listener in listeners:
                listeners.remove(listener)

        return remove

    def start(self) -> None:
        if not self._stopped:
            return
        self._stopped = False
        self._connect()

    def stop(self) -> None:
        """Closes the socket with 1000 Normal Closure and stops reconnecting."""
        self._stopped = True
        self._cancel_backoff()
        self._teardown(1000, "client_stop")
        self._set_state("STOPPED")

    async def aclose(self) -> None:
        """:meth:`stop` and wait for background work (socket closes, refreshes) to settle."""
        if self._state != "STOPPED":
            self.stop()
        current = asyncio.current_task()
        tasks = [t for t in self._tasks if t is not current]
        for t in tasks:
            if t not in self._close_tasks:
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def mark_ready(self, degraded: bool, epoch: int | None = None) -> None:
        """Marks resync completion (called by the high-level client after gap recovery)."""
        if epoch is not None and epoch != self._epoch:
            return
        if self._state in _CONNECTED_STATES:
            self._set_state("READY_DEGRADED" if degraded else "READY")
            self._attempt = 0

    def send(self, frame: Mapping[str, Any]) -> int:
        """Sends a frame without waiting for ``reply_to``. Returns the allocated id.

        The write itself is queued on the event loop (in order); this call does not block.
        """
        ws = self._ws
        if ws is None or not self.is_connected():
            raise GabrielError("SOCKET_CLOSED", "WebSocket is not connected")
        frame_id = self._seq
        self._seq += 1
        data = json.dumps({**frame, "id": frame_id}, separators=(",", ":"), ensure_ascii=False)
        spawn(self._write(ws, data), self._tasks)
        return frame_id

    async def _write(self, ws: WebSocketLike, data: str) -> None:
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        try:
            async with self._send_lock:
                await ws.send(data)
        except Exception as err:
            # Socket closing; the reader notices the close and reconnects.
            self._logger.warn({"err": scrub_secrets(str(err))}, "websocket send failed")

    async def send_command(self, frame: Mapping[str, Any], timeout: float | None = None) -> RawWsFrame:
        """Sends a frame and resolves with the matching ``reply_to`` frame."""
        timeout = self.timings.command_timeout if timeout is None else timeout
        frame_id = self.send(frame)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[RawWsFrame] = loop.create_future()
        frame_type = frame.get("type")

        def on_timeout() -> None:
            self._pending.pop(frame_id, None)
            if not future.done():
                future.set_exception(
                    GabrielError("NETWORK_TIMEOUT", f"no reply_to for {frame_type} within {round(timeout * 1000)}ms")
                )

        timer = loop.call_later(timeout, on_timeout)
        self._pending[frame_id] = _Pending(future, timer, self._epoch)
        try:
            return await future
        finally:
            timer.cancel()
            self._pending.pop(frame_id, None)

    def is_connected(self) -> bool:
        return self._state in _CONNECTED_STATES

    def reconnect(self, reason: str, *, cold: bool = False) -> None:
        """Forces a reconnect (e.g. after a token refresh)."""
        if cold:
            self._clear_fast_reconnect()
        self._teardown(4000, reason)
        self._schedule_reconnect(True)

    # -- connection ---------------------------------------------------------------------------

    def _connect(self) -> None:
        if self._stopped:
            return
        try:
            session = self._auth.current()
        except Exception:
            self._set_state("AUTH_DEAD")
            return
        use_fast = (
            self._frt_url is not None
            and not self._fast_failed
            and self._frt_token == session.token
            and self._now() - self._frt_received_at <= self.timings.fast_reconnect_ttl
        )
        url = (
            self._frt_url if use_fast and self._frt_url is not None else self._cold_url(session.token, session.team_id)
        )
        self._connecting_fast = use_fast
        self._epoch += 1
        epoch = self._epoch
        self._set_state("CONNECTING_FAST" if use_fast else "CONNECTING_COLD")
        headers = {
            "Cookie": f"d={session.cookie_d}",
            "Origin": self._metadata.origin,
            "User-Agent": self._metadata.user_agent,
        }
        self._run_task = spawn(self._run(epoch, url, headers), self._tasks)

    async def _run(self, epoch: int, url: str, headers: dict[str, str]) -> None:
        """Opens the socket for ``epoch`` and pumps frames until it closes or is superseded."""
        try:
            ws = await self._factory(url, headers)
        except asyncio.CancelledError:
            raise
        except Exception as err:
            if epoch != self._epoch:
                return
            rejection = _handshake_rejection(err)
            if rejection is not None:
                self._on_unexpected_response(rejection[0], rejection[1])
                return
            self._logger.warn({"err": scrub_secrets(str(err))}, "websocket creation failed")
            self._on_socket_gone(epoch, 1006)
            return
        if epoch != self._epoch:
            # Superseded (stop / reconnect) while the handshake was in flight.
            await self._close_socket(ws, 1000, "superseded")
            return
        self._ws = ws
        self._on_open(epoch)
        while True:
            try:
                data = await ws.recv()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                if epoch != self._epoch:
                    return
                if not _is_connection_closed(err):
                    self._logger.warn({"err": scrub_secrets(str(err))}, "websocket error")
                self._on_socket_gone(epoch, _close_code(ws, err))
                return
            if epoch != self._epoch:
                return
            self._on_message(epoch, data)
            if epoch != self._epoch:
                return

    def _on_open(self, epoch: int) -> None:
        self._set_state("AWAITING_HELLO")
        self._last_frame_at = self._now()

        def on_hello_timeout() -> None:
            if epoch != self._epoch:
                return
            self._logger.warn({"epoch": epoch}, "no hello within timeout")
            if self._connecting_fast:
                self._clear_fast_reconnect()
            self._teardown(4000, "hello_timeout")
            self._schedule_reconnect(False)

        self._hello_timer = asyncio.get_running_loop().call_later(self.timings.hello_timeout, on_hello_timeout)

    def _on_message(self, epoch: int, data: str | bytes) -> None:
        self._last_frame_at = self._now()
        try:
            frame = json.loads(data)
        except ValueError:
            self._logger.warn({"epoch": epoch}, "dropping non-JSON frame")
            return
        if not isinstance(frame, dict):
            self._logger.warn({"epoch": epoch}, "dropping non-object frame")
            return
        self._handle_frame(frame, epoch)

    def _on_unexpected_response(self, status: int, headers: Mapping[str, Any]) -> None:
        self._logger.warn({"status": status}, "websocket handshake rejected")
        if status in (401, 403):
            self._teardown(4000, "handshake_auth")
            spawn(self._refresh_credentials("invalid_auth"), self._tasks)
            return
        if status == 429:
            try:
                retry_after = float(_header(headers, "retry-after") or 0)
            except (TypeError, ValueError):
                retry_after = 0.0
            self._next_delay_override = retry_after if retry_after > 0 else self.timings.server_busy_default
        if self._connecting_fast:
            self._clear_fast_reconnect()
        self._teardown(4000, "handshake_failed")
        self._schedule_reconnect(False)

    def _cold_url(self, token: str, team_id: str) -> str:
        params = {
            "token": token,
            "sync_desync": "1",
            "slack_client": "desktop",
            "start_args": build_start_args(self._metadata.agent_version),
            "no_query_on_subscribe": "1",
            "flannel": "3",
            "lazy_channels": "1",
            "gateway_server": self._gateway_server or f"{team_id}-1",
            "batch_presence_aware": "1",
        }
        return f"{self._endpoints.ws_base}?{urlencode(params)}"

    # -- frames -------------------------------------------------------------------------------

    def _handle_frame(self, frame: RawWsFrame, epoch: int) -> None:
        reply_to = frame.get("reply_to")
        if isinstance(reply_to, int) and not isinstance(reply_to, bool):
            pending = self._pending.pop(reply_to, None)
            if pending is not None:
                pending.timer.cancel()
                if not pending.future.done():
                    if frame.get("ok") is False:
                        raw_error = frame.get("error")
                        err_obj = raw_error if isinstance(raw_error, dict) else {}
                        msg = err_obj.get("msg")
                        raw_slack = msg if isinstance(msg, str) else (raw_error if isinstance(raw_error, str) else None)
                        rejected = GabrielError(
                            "SLACK_API_ERROR",
                            f"ws command rejected: {msg if msg is not None else 'error'}",
                            raw_slack_error=raw_slack,
                        )
                        # The reply frame carries gateway error codes (Node: `cause`); callers such as
                        # the presence pool read it from `err.frame`.
                        setattr(rejected, "frame", frame)  # noqa: B010
                        pending.future.set_exception(rejected)
                    else:
                        pending.future.set_result(frame)

        frame_type = frame.get("type")
        if frame_type == "hello":
            self._cancel_hello_timer()
            fast_reconnect = frame.get("fast_reconnect") is True
            self._fast_failed = False
            self._set_state("RESYNCING")
            self._start_heartbeat(epoch)
            self._metrics.increment("ws_hello_total", {"fast": "true" if fast_reconnect else "false"})
            self._emit("hello", HelloInfo(epoch=epoch, fast_reconnect=fast_reconnect, frame=frame))
        elif frame_type == "reconnect_url":
            url = frame.get("url")
            if isinstance(url, str):
                self._frt_url = url
                self._frt_received_at = self._now()
                self._frt_token = self._safe_token()
        elif frame_type == "goodbye":
            self._logger.info({"epoch": epoch}, "server goodbye; switching connection")
            self._teardown(4104, "goodbye")
            self._schedule_reconnect(True)
        elif frame_type == "error":
            self._handle_error_frame(frame)

        for listener in list(self._listeners["frame"]):
            try:
                listener(frame, epoch)
            except Exception as err:
                self._logger.error({"err": str(err)}, "frame listener threw")

    def _handle_error_frame(self, frame: RawWsFrame) -> None:
        raw_error = frame.get("error")
        err = raw_error if isinstance(raw_error, dict) else {}
        raw_msg = err.get("msg")
        msg = raw_msg if isinstance(raw_msg, str) else ""
        self._logger.warn({"msg": msg, "code": err.get("code")}, "websocket error frame")
        if msg in _FRT_INVALID_MESSAGES or (msg == "invalid_auth" and self._connecting_fast):
            self._clear_fast_reconnect()
            return
        if msg == "invalid_auth":
            self._teardown(4000, "invalid_auth")
            spawn(self._refresh_credentials("invalid_auth"), self._tasks)
            return
        if msg in _SERVER_BUSY_MESSAGES:
            retry_after = err.get("retry_after")
            if isinstance(retry_after, int | float) and not isinstance(retry_after, bool) and retry_after:
                self._next_delay_override = float(retry_after)
            else:
                self._next_delay_override = self.timings.server_busy_default

    async def _refresh_credentials(self, trigger: RefreshTrigger) -> None:
        self._set_state("CREDENTIAL_REFRESH")
        failed_token = self._safe_token()
        try:
            await self._auth.refresh(trigger, failed_token)
        except GabrielError as err:
            if err.code == "AUTH_DEAD":
                self._stopped = True
                self._set_state("AUTH_DEAD")
                return
            self._schedule_reconnect(False)
            return
        except Exception:
            self._schedule_reconnect(False)
            return
        self._clear_fast_reconnect()
        self._cancel_backoff()
        self._attempt = 0
        self._set_state("DISCONNECTED")
        self._connect()

    def _safe_token(self) -> str | None:
        try:
            return self._auth.current().token
        except Exception:
            return None

    # -- heartbeat ----------------------------------------------------------------------------

    def _start_heartbeat(self, epoch: int) -> None:
        self._stop_heartbeat()

        def ping() -> None:
            if epoch != self._epoch:
                return
            try:
                self.send({"type": "ping"})
            except Exception:
                pass  # socket closing; the reader reconnects

        def watchdog() -> None:
            if epoch != self._epoch or self._last_frame_at is None:
                return
            age = self._now() - self._last_frame_at
            if age > self.timings.dead_peer_timeout:
                self._logger.warn({"epoch": epoch, "lastFrameAgeMs": round(age * 1000)}, "dead peer detected")
                self._teardown(4000, "dead_peer")
                self._schedule_reconnect(True)

        self._ping_timer = _Interval(self.timings.ping_interval, ping)
        self._watchdog_timer = _Interval(self.timings.watchdog_interval, watchdog)

    def _stop_heartbeat(self) -> None:
        if self._ping_timer is not None:
            self._ping_timer.cancel()
        if self._watchdog_timer is not None:
            self._watchdog_timer.cancel()
        self._ping_timer = None
        self._watchdog_timer = None

    # -- teardown / reconnect -----------------------------------------------------------------

    def _on_socket_gone(self, epoch: int, code: int) -> None:
        was_connected = self.is_connected()
        if self._connecting_fast and not was_connected:
            self._clear_fast_reconnect()
        self._teardown(code, "closed")
        self._emit("closed", ClosedInfo(epoch=epoch, code=code))
        if self._stopped or self._state in ("AUTH_DEAD", "CREDENTIAL_REFRESH"):
            return
        self._schedule_reconnect(was_connected)

    def _teardown(self, code: int, reason: str) -> None:
        """Detaches the current socket, rejects pending commands and stops timers."""
        self._cancel_hello_timer()
        self._stop_heartbeat()
        ws = self._ws
        task = self._run_task
        self._ws = None
        self._run_task = None
        in_flight = task is not None and not task.done()
        # Invalidate the epoch so late callbacks from the old socket are ignored.
        if ws is not None or in_flight:
            self._epoch += 1
            if task is not None and in_flight and task is not asyncio.current_task():
                task.cancel()
            if ws is not None:
                closing = spawn(self._close_socket(ws, code, reason), self._tasks)
                self._close_tasks.add(closing)
                closing.add_done_callback(self._close_tasks.discard)
        for frame_id, pending in list(self._pending.items()):
            pending.timer.cancel()
            if not pending.future.done():
                pending.future.set_exception(GabrielError("SOCKET_CLOSED", "WebSocket closed before reply"))
            self._pending.pop(frame_id, None)

    async def _close_socket(self, ws: WebSocketLike, code: int, reason: str) -> None:
        try:
            await ws.close(code, reason)
        except Exception:
            pass  # already closed / not a sendable close code

    def _clear_fast_reconnect(self) -> None:
        self._frt_url = None
        self._frt_token = None
        self._fast_failed = True

    def _schedule_reconnect(self, immediate: bool) -> None:
        """``immediate`` skips backoff for a graceful hand-over from a healthy connection."""
        if self._stopped:
            return
        self._cancel_backoff()
        override = self._next_delay_override
        self._next_delay_override = None
        # Only a drop from a healthy (READY) connection reconnects without delay; repeated
        # failures before READY fall back to full-jitter backoff to avoid a reconnect storm.
        if override is not None:
            delay = override
        elif immediate and self._attempt == 0:
            delay = 0.0
        else:
            delay = full_jitter_delay(self._attempt, self.timings.backoff_base, self.timings.backoff_cap, self._random)
        self._attempt += 1
        self._set_state("BACKOFF")
        self._metrics.increment("ws_reconnect_total")
        self._emit("reconnecting", ReconnectingInfo(attempt=self._attempt, delay=delay))

        def fire() -> None:
            self._backoff_timer = None
            self._set_state("DISCONNECTED")
            self._connect()

        self._backoff_timer = asyncio.get_running_loop().call_later(delay, fire)

    def _cancel_backoff(self) -> None:
        if self._backoff_timer is not None:
            self._backoff_timer.cancel()
            self._backoff_timer = None

    def _cancel_hello_timer(self) -> None:
        if self._hello_timer is not None:
            self._hello_timer.cancel()
            self._hello_timer = None

    def _set_state(self, next_state: WsState) -> None:
        prev = self._state
        if prev == next_state:
            return
        self._state = next_state
        for listener in list(self._listeners["state"]):
            try:
                listener(next_state, prev)
            except Exception as err:
                self._logger.error({"err": str(err)}, "state listener threw")

    def _emit(self, event: WsEventName, payload: Any) -> None:
        for listener in list(self._listeners[event]):
            try:
                listener(payload)
            except Exception as err:
                self._logger.error({"err": str(err), "event": event}, "ws listener threw")
