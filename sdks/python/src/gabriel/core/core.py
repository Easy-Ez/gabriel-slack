"""Low-level protocol core: fidelity, reliability and zero business assumptions."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from types import TracebackType
from typing import Any, Self
from urllib.parse import urljoin

import httpx

from ..errors import GabrielError
from ..util.logger import Logger, silent_logger
from .auth import (
    AuthDeps,
    AuthEvent,
    AuthManager,
    AuthOptions,
    CookieExchangeResult,
    TokenIdentity,
    WorkspaceSession,
    exchange_cookie,
    find_team,
)
from .http import HttpEngine
from .metadata import DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS, ClientMetadata, Endpoints
from .types import MetricsSink, RawWsFrame, RequestOpts, WebSocketFactory, noop_metrics
from .ws import HelloInfo, WsConnection, WsState, WsTimings, default_ws_factory

#: Frame types the reference implementation knows about; anything else is reported as protocol drift.
KNOWN_FRAME_TYPES: frozenset[str] = frozenset(
    {
        "hello", "pong", "reconnect_url", "goodbye", "error",
        "message", "presence_change", "manual_presence_change", "user_typing",
        "reaction_added", "reaction_removed", "user_change", "user_invalidated", "user_status_changed",
        "dnd_updated", "dnd_updated_user", "dnd_invalidated",
        "subteam_created", "subteam_updated", "subteam_members_changed", "subteam_self_added", "subteam_self_removed",
        "channel_marked", "im_marked", "group_marked", "mpim_marked", "thread_marked",
        "activity", "badge_counts_updated", "clear_mention_notification", "desktop_notification",
        "draft_create", "draft_change", "draft_send", "draft_delete",
        "member_joined_channel", "member_left_channel", "channel_joined", "channel_left", "group_joined", "group_left",
        "im_created", "im_open", "im_close", "mpim_open", "mpim_close", "mpim_joined",
        "channel_created", "channel_deleted", "channel_rename", "group_rename", "channel_archive", "group_archive",
        "channel_unarchive", "group_unarchive", "channel_updated", "channel_history_changed",
        "file_shared", "file_change", "file_public", "file_deleted", "file_created", "file_unshared",
        "file_comment_added", "file_comment_edited", "file_comment_deleted",
        "pin_added", "pin_removed", "star_added", "star_removed", "emoji_changed", "pref_change",
        "subscription_update", "flannel", "ai_assistant_status", "agent_session",
        "update_thread_state", "thread_subscribed", "thread_unsubscribed",
        "user_huddle_changed", "sh_room_join", "sh_room_leave", "sh_room_update",
        "team_join", "team_rename", "team_pref_change", "team_profile_change", "bot_added", "bot_changed",
        "apps_changed", "apps_installed", "apps_uninstalled", "commands_changed",
        "mobile_in_app_notification", "reminder_added", "reminder_fired", "reminder_completed",
        # Observed on live connections (client bookkeeping, no high-level event).
        "visits_changed", "file_view_ready",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class ProtocolDriftInfo:
    unknown_type: str
    sample_frame: RawWsFrame
    unknown_subtype: str | None = None


def _new_http_client() -> httpx.AsyncClient:
    # Like fetch: no cookie jar (cookies are only ever sent explicitly) and no implicit redirects.
    no_cookies = CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))
    return httpx.AsyncClient(cookies=no_cookies, follow_redirects=False)


def _merge(base: Any, override: Any) -> Any:
    if override is None:
        return base
    if isinstance(override, Mapping):
        return dataclasses.replace(base, **dict(override))
    return override


class _AuthHelpers:
    """Credential helpers (``exchange_cookie`` returns tokens — never log the result)."""

    def __init__(self, core: GabrielCore) -> None:
        self._core = core

    def _deps(self) -> AuthDeps:
        c = self._core
        return AuthDeps(http_client=c._http_client, metadata=c.metadata, endpoints=c.endpoints)

    async def exchange_cookie(self, cookie_d: str) -> CookieExchangeResult:
        return await exchange_cookie(self._deps(), cookie_d)

    async def find_team(self, domain: str) -> dict[str, Any]:
        return await find_team(self._deps(), domain)

    async def test(self) -> dict[str, Any]:
        return await self._core.call_web_api("auth.test", {})


class GabrielCore:
    """Low-level protocol core: fidelity, reliability and zero business assumptions.

    Frames are delivered unmodified; the high-level client is built entirely on these
    primitives and exposes them as ``client.core`` for escape-hatch use.

    Usable as ``async with GabrielCore(...) as core:`` (connects on enter, closes on exit).
    """

    def __init__(
        self,
        *,
        auth: AuthOptions,
        metadata: ClientMetadata | Mapping[str, str] | None = None,
        endpoints: Endpoints | Mapping[str, str | None] | None = None,
        http_client: httpx.AsyncClient | None = None,
        ws_factory: WebSocketFactory | None = None,
        gateway_server: str | None = None,
        logger: Logger | None = None,
        metrics: MetricsSink | None = None,
        max_http_retries: int | None = None,
        ws_timings: WsTimings | None = None,
    ) -> None:
        """
        Args:
            auth: Session credentials.
            metadata: Client identity; a mapping overrides individual default fields.
            endpoints: Network endpoints; a mapping overrides individual default fields.
            http_client: Custom ``httpx.AsyncClient`` (proxies, tests). Not closed by :meth:`close`.
            ws_factory: Custom WebSocket factory (proxies, tests). Defaults to ``websockets``.
            gateway_server: Gateway shard override, defaults to ``{team_id}-1``.
            max_http_retries: Max automatic retries for transient HTTP failures (default 3).
            ws_timings: WebSocket protocol timings (tests only; defaults follow the spec).
        """
        self._logger: Logger = logger or silent_logger
        self.metadata: ClientMetadata = _merge(DEFAULT_CLIENT_METADATA, metadata)
        self.endpoints: Endpoints = _merge(DEFAULT_ENDPOINTS, endpoints)
        self._owns_http_client = http_client is None
        self._http_client = http_client if http_client is not None else _new_http_client()
        resolved_metrics = metrics or noop_metrics
        self.auth_manager = AuthManager(
            auth,
            AuthDeps(
                http_client=self._http_client, metadata=self.metadata, endpoints=self.endpoints, logger=self._logger
            ),
        )
        self.http = HttpEngine(
            http_client=self._http_client,
            auth=self.auth_manager,
            metadata=self.metadata,
            endpoints=self.endpoints,
            logger=self._logger,
            metrics=resolved_metrics,
            max_retries=3 if max_http_retries is None else max_http_retries,
        )
        self.ws = WsConnection(
            auth=self.auth_manager,
            metadata=self.metadata,
            endpoints=self.endpoints,
            ws_factory=ws_factory or default_ws_factory,
            logger=self._logger,
            metrics=resolved_metrics,
            gateway_server=gateway_server,
            timings=ws_timings,
        )
        #: Credential helpers: ``exchange_cookie(cookie_d)``, ``find_team(domain)``, ``test()``.
        self.auth = _AuthHelpers(self)
        self._raw_handlers: list[Callable[[RawWsFrame], None]] = []
        self._type_handlers: dict[str, list[Callable[[RawWsFrame], None]]] = {}
        self._subtype_handlers: dict[str, list[Callable[[RawWsFrame], None]]] = {}
        self._drift_handlers: list[Callable[[ProtocolDriftInfo], None]] = []
        self._seen_unknown: set[str] = set()
        self._started = False
        self.ws.on("frame", lambda frame, _epoch: self._dispatch_frame(frame))

    def __repr__(self) -> str:
        return f"GabrielCore(auth_state={self.auth_manager.state!r}, ws_state={self.ws.state!r})"

    @property
    def session(self) -> WorkspaceSession:
        return self.auth_manager.current()

    @property
    def ws_state(self) -> WsState:
        return self.ws.state

    async def initialize(self) -> WorkspaceSession:
        """Resolves the workspace session. Idempotent."""
        if self.auth_manager.state != "UNINITIALIZED":
            return self.auth_manager.current()

        async def verify(provisional: WorkspaceSession) -> TokenIdentity:
            # Token supplied explicitly: verify the pairing and derive identity via auth.test.
            try:
                resp = await self._http_client.post(
                    urljoin(provisional.api_base, "api/auth.test"),
                    headers={
                        "Cookie": f"d={provisional.cookie_d}",
                        "Origin": self.metadata.origin,
                        "User-Agent": self.metadata.user_agent,
                    },
                    data={"token": provisional.token},
                    timeout=15.0,
                )
            except Exception as err:
                raise GabrielError("NETWORK_TIMEOUT", "auth.test request failed", cause=err) from err
            try:
                body: Any = resp.json()
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            team_id, user_id = body.get("team_id"), body.get("user_id")
            if (
                not body.get("ok")
                or not isinstance(team_id, str)
                or not team_id
                or not isinstance(user_id, str)
                or not user_id
            ):
                detail = body.get("error") or f"HTTP {resp.status_code}"
                raise GabrielError("AUTH_DEAD", f"auth.test rejected the supplied token ({detail})")
            return TokenIdentity(team_id=team_id, user_id=user_id)

        return await self.auth_manager.initialize(verify)

    async def connect(self) -> None:
        """Initializes auth (if needed) and starts the WebSocket state machine."""
        await self.initialize()
        if self._started:
            return
        self._started = True
        self.ws.start()

    def disconnect(self) -> None:
        self._started = False
        self.ws.stop()

    async def close(self) -> None:
        """:meth:`disconnect`, wait for sockets to close, and release the owned HTTP client."""
        self._started = False
        await self.ws.aclose()
        if self._owns_http_client:
            await self._http_client.aclose()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.close()

    async def call_web_api(
        self, method: str, fields: dict[str, Any] | None = None, opts: RequestOpts | None = None
    ) -> dict[str, Any]:
        return await self.http.call_web_api(method, fields, opts)

    async def call_edge_api(
        self, endpoint: str, payload: dict[str, Any] | None = None, opts: RequestOpts | None = None
    ) -> dict[str, Any]:
        return await self.http.call_edge_api(endpoint, payload, opts)

    async def send_ws_command(self, frame: dict[str, Any], timeout: float | None = None) -> RawWsFrame:
        """Sends an upstream command, allocating ``id`` and awaiting ``reply_to`` (default 10 s)."""
        return await self.ws.send_command(frame, timeout)

    def send_ws_frame(self, frame: dict[str, Any]) -> int:
        """Sends an upstream frame without awaiting a reply. Returns the allocated id."""
        return self.ws.send(frame)

    def on_raw_frame(self, handler: Callable[[RawWsFrame], None]) -> Callable[[], None]:
        return _subscribe(self._raw_handlers, handler)

    def on_event(self, type: str, handler: Callable[[RawWsFrame], None]) -> Callable[[], None]:
        return _subscribe(self._type_handlers.setdefault(type, []), handler)

    def on_message_subtype(self, subtype: str | None, handler: Callable[[RawWsFrame], None]) -> Callable[[], None]:
        """``subtype=None`` subscribes to plain messages without a subtype."""
        return _subscribe(self._subtype_handlers.setdefault(subtype or "", []), handler)

    def on_protocol_drift(self, handler: Callable[[ProtocolDriftInfo], None]) -> Callable[[], None]:
        return _subscribe(self._drift_handlers, handler)

    def on_hello(self, handler: Callable[[HelloInfo], None]) -> Callable[[], None]:
        return self.ws.on("hello", handler)

    def on_auth_event(self, handler: Callable[[AuthEvent], None]) -> Callable[[], None]:
        return self.auth_manager.on_event(handler)

    def _dispatch_frame(self, frame: RawWsFrame) -> None:
        frame_type = frame.get("type")

        def safe(fn: Callable[[RawWsFrame], None]) -> None:
            try:
                fn(frame)
            except Exception as err:
                self._logger.error({"err": str(err), "type": frame_type}, "raw frame handler threw")

        for h in list(self._raw_handlers):
            safe(h)
        if isinstance(frame_type, str) and frame_type:
            for h in list(self._type_handlers.get(frame_type, ())):
                safe(h)
            if frame_type == "message":
                raw_sub = frame.get("subtype")
                sub = raw_sub if isinstance(raw_sub, str) else ""
                for h in list(self._subtype_handlers.get(sub, ())):
                    safe(h)
            if frame_type not in KNOWN_FRAME_TYPES:
                self.report_drift(frame_type, None, frame)

    def report_drift(self, unknown_type: str, unknown_subtype: str | None, frame: RawWsFrame) -> None:
        """Reports an unknown frame type/subtype once per process."""
        key = f"{unknown_type}/{unknown_subtype or ''}"
        if key in self._seen_unknown:
            return
        self._seen_unknown.add(key)
        self._logger.info(
            {"unknownType": unknown_type, "unknownSubtype": unknown_subtype}, "protocol drift: unknown frame"
        )
        info = ProtocolDriftInfo(unknown_type=unknown_type, unknown_subtype=unknown_subtype, sample_frame=frame)
        for h in list(self._drift_handlers):
            try:
                h(info)
            except Exception as err:
                self._logger.error({"err": str(err)}, "drift handler threw")


def _subscribe(handlers: list[Any], handler: Any) -> Callable[[], None]:
    handlers.append(handler)

    def remove() -> None:
        if handler in handlers:
            handlers.remove(handler)

    return remove
