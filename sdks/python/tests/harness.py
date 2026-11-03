"""In-memory Slack for tests: Web API, EdgeAPI, cookie exchange and WebSocket gateway.

Port of ``sdks/node/test/harness.ts``. HTTP goes through ``httpx.MockTransport`` (see
:attr:`FakeSlack.http_client`); WebSockets through :meth:`FakeSlack.create_web_socket`, an
async :data:`~gabriel.core.types.WebSocketFactory` returning :class:`FakeSocket`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from gabriel.core.auth import AuthOptions
from gabriel.core.metadata import Endpoints
from gabriel.core.types import RawWsFrame
from gabriel.core.ws import WebSocketHandshakeError

TEAM = "T0123456789"
SELF = "U0SELF00001"
TOKEN_1 = "xoxc-test-token-1"
COOKIE = "xoxd-test-cookie-value"

ENDPOINTS = Endpoints(
    auth_url="https://app.slack.test/auth?app=client&response_type=json",
    public_api_base="https://slack.test/api/",
    edge_api_base="https://edgeapi.slack.test/cache/",
    ws_base="wss://ws.slack.test/",
)


class FakeConnectionClosed(Exception):
    """Raised by :meth:`FakeSocket.recv` once the socket is closed (like websockets' ConnectionClosed)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"connection closed ({code})")
        self.code = code


@dataclass(frozen=True, slots=True)
class _Closed:
    code: int


class FakeSocket:
    """A server-controlled :class:`~gabriel.core.types.WebSocketLike`.

    Client side: ``send`` / ``recv`` / ``close``. Server side (test helpers): :meth:`open`,
    :meth:`deliver`, :meth:`deliver_raw`, :meth:`server_close`, :meth:`reject`.
    """

    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.headers = headers
        #: 0 connecting, 1 open, 3 closed (WebSocket readyState values).
        self.ready_state = 0
        self.sent: list[RawWsFrame] = []
        #: Close code passed by the client's ``close()``; None if the client never closed it.
        self.closed_with: int | None = None
        self.close_code: int | None = None
        self._inbox: asyncio.Queue[str | _Closed] = asyncio.Queue()
        self._opened: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._sent_listeners: list[Callable[[RawWsFrame], None]] = []

    # -- WebSocketLike ---------------------------------------------------------------------

    async def send(self, data: str) -> None:
        frame = json.loads(data)
        self.sent.append(frame)
        for listener in list(self._sent_listeners):
            listener(frame)

    async def recv(self) -> str:
        item = await self._inbox.get()
        if isinstance(item, _Closed):
            self._inbox.put_nowait(item)  # stay closed for later recv() calls
            raise FakeConnectionClosed(item.code)
        return item

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.ready_state == 3:
            return
        self.ready_state = 3
        self.closed_with = code
        self.close_code = code
        self._inbox.put_nowait(_Closed(code))
        if not self._opened.done():
            self._opened.set_exception(ConnectionError("closed before open"))
            self._opened.exception()

    # -- server-side helpers ---------------------------------------------------------------

    def open(self) -> None:
        """Completes the handshake (the factory returns the socket)."""
        self.ready_state = 1
        if not self._opened.done():
            self._opened.set_result(None)

    def reject(self, status: int, headers: dict[str, str] | None = None) -> None:
        """Rejects the handshake with an HTTP status (Node's ``unexpected-response``)."""
        self.ready_state = 3
        if not self._opened.done():
            self._opened.set_exception(WebSocketHandshakeError(status, headers))
            self._opened.exception()

    def deliver(self, frame: RawWsFrame) -> None:
        self._inbox.put_nowait(json.dumps(frame))

    def deliver_raw(self, data: str) -> None:
        self._inbox.put_nowait(data)

    def server_close(self, code: int = 1006) -> None:
        """Server drops the connection. Before :meth:`open` this fails the handshake."""
        self.ready_state = 3
        self.close_code = code
        if not self._opened.done():
            self._opened.set_exception(ConnectionError(f"connection failed ({code})"))
            self._opened.exception()
            return
        self._inbox.put_nowait(_Closed(code))

    def sent_of_type(self, type: str) -> list[RawWsFrame]:
        return [f for f in self.sent if f.get("type") == type]

    def on_sent(self, listener: Callable[[RawWsFrame], None]) -> Callable[[], None]:
        self._sent_listeners.append(listener)
        return lambda: self._sent_listeners.remove(listener)

    async def wait_open(self) -> None:
        await self._opened


#: ``handler(fields, call_number)`` → JSON-able dict, an ``httpx.Response``, or an awaitable of either.
ApiHandler = Callable[[dict[str, str], int], Any]


@dataclass(slots=True)
class ApiCall:
    method: str
    fields: dict[str, str]


@dataclass(slots=True)
class EdgeCall:
    endpoint: str
    body: dict[str, Any]


@dataclass(slots=True)
class FileBody:
    body: bytes
    content_type: str


def json_response(body: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, json=body, headers=headers)


def parse_multipart(content_type: str, body: bytes) -> dict[str, str]:
    """Parses the SDK's multipart body; line breaks are normalized back from CRLF."""
    m = re.search(r"boundary=(\S+)", content_type)
    if m is None:
        raise ValueError(f"not multipart: {content_type}")
    delimiter = f"--{m.group(1)}"
    fields: dict[str, str] = {}
    for part in body.decode("utf-8").split(delimiter)[1:]:
        if part.startswith("--"):
            break
        head, _, value = part.removeprefix("\r\n").partition("\r\n\r\n")
        name_match = re.search(r'name="([^"]*)"', head)
        if name_match is None:
            continue
        name = name_match.group(1).replace("%0A", "\n").replace("%0D", "\r").replace("%22", '"')
        # Multipart encoding turns line breaks into CRLF; the server normalizes them back.
        fields[name] = value.removesuffix("\r\n").replace("\r\n", "\n")
    return fields


class FakeSlack:
    """In-memory Slack: Web API, EdgeAPI, cookie exchange and WebSocket gateway.

    * Configure Web API responses with :meth:`on`; inspect requests via :attr:`calls`,
      :meth:`calls_to`, :attr:`edge_calls`, :attr:`auth_calls`.
    * Rotate credentials with :attr:`token`; kill the cookie with ``auth_alive = False``.
    * Sockets: :attr:`sockets` / :attr:`socket` (latest). With ``auto_hello`` (default) each
      socket opens and receives ``hello`` immediately; otherwise drive it with
      :meth:`FakeSocket.open` / :meth:`FakeSocket.deliver` / :meth:`FakeSocket.server_close`.
    * Override :meth:`handle` (e.g. ``slack.handle = custom``) to intercept any HTTP request.
    """

    def __init__(self) -> None:
        self.sockets: list[FakeSocket] = []
        self.calls: list[ApiCall] = []
        self.edge_calls: list[EdgeCall] = []
        self.auth_calls = 0
        self.token = TOKEN_1
        self.auth_alive = True
        self.auto_hello = True
        self.hello_fast: bool | Callable[[int], bool] = False
        self.users: dict[str, dict[str, Any]] = {}
        self.channels: dict[str, dict[str, Any]] = {}
        self.file_bodies: dict[str, FileBody] = {}
        self.posted_ts = 1_800_000_000
        self._api: dict[str, ApiHandler] = {}
        self._api_counts: dict[str, int] = {}
        self.channels["C0GENERAL01"] = {"id": "C0GENERAL01", "name": "general", "is_channel": True, "is_private": False}
        self.users[SELF] = {"id": SELF, "name": "me", "team_id": TEAM, "profile": {"display_name": "Me"}}
        self.users["U0ALICE0001"] = {
            "id": "U0ALICE0001",
            "name": "alice",
            "team_id": TEAM,
            "profile": {"display_name": "Alice", "status_text": "", "status_emoji": ""},
        }
        self.users["U0BOB000001"] = {
            "id": "U0BOB000001",
            "name": "bob",
            "team_id": TEAM,
            "profile": {"display_name": "Bob"},
        }
        self.on(
            "usergroups.list",
            lambda _f, _n: {
                "ok": True,
                "usergroups": [{"id": "S0ONCALL001", "handle": "oncall", "users": [SELF, "U0BOB000001"]}],
            },
        )
        self.on("chat.postMessage", self._post_message)
        self.on("chat.update", lambda f, _n: {"ok": True, "channel": f.get("channel"), "ts": f.get("ts")})
        self.on("client.counts", lambda _f, _n: {"ok": True, "channels": [], "mpims": [], "ims": []})
        self.on("conversations.replies", lambda _f, _n: {"ok": True, "messages": [], "has_more": False})
        self.on("reactions.add", lambda _f, _n: {"ok": True})
        self.transport = httpx.MockTransport(self._dispatch)
        self.http_client = httpx.AsyncClient(transport=self.transport)

    def _post_message(self, f: dict[str, str], _n: int) -> dict[str, Any]:
        self.posted_ts += 1
        ts = f"{self.posted_ts}.000100"
        message: dict[str, Any] = {"ts": ts, "text": f.get("markdown_text") or f.get("text") or "", "user": SELF}
        if f.get("blocks"):
            message["blocks"] = json.loads(f["blocks"])
        return {"ok": True, "channel": f.get("channel"), "ts": ts, "message": message}

    def on(self, method: str, handler: ApiHandler) -> FakeSlack:
        self._api[method] = handler
        return self

    def calls_to(self, method: str) -> list[ApiCall]:
        return [c for c in self.calls if c.method == method]

    @property
    def socket(self) -> FakeSocket:
        if not self.sockets:
            raise AssertionError("no socket yet")
        return self.sockets[-1]

    async def aclose(self) -> None:
        await self.http_client.aclose()

    # -- HTTP --------------------------------------------------------------------------------

    async def _dispatch(self, request: httpx.Request) -> httpx.Response:
        # Indirection so tests can replace ``slack.handle``.
        return await self.handle(request)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        url = request.url
        host = url.host
        if host == "app.slack.test":
            self.auth_calls += 1
            await asyncio.sleep(0.005)
            if not self.auth_alive:
                return httpx.Response(302, headers={"location": "https://slack.test/signin"})
            return json_response(
                {
                    "teams": {
                        TEAM: {
                            "id": TEAM,
                            "name": "Test",
                            "domain": "test",
                            "url": "https://test.slack.com/",
                            "token": self.token,
                            "user_id": SELF,
                        }
                    },
                    "lc": "1",
                }
            )
        if host == "edgeapi.slack.test":
            endpoint = "/".join(url.path.split("/")[3:])
            body: dict[str, Any] = json.loads(request.content or b"{}")
            self.edge_calls.append(EdgeCall(endpoint, body))
            if body.get("token") != self.token:
                return json_response({"ok": False, "error": "invalid_auth"})
            ids = list((body.get("updated_ids") or {}).keys())
            source = self.users if endpoint == "users/info" else self.channels
            return json_response({"ok": True, "results": [source[i] for i in ids if i in source]})
        if host == "test.slack.com":
            method = url.path.replace("/api/", "", 1)
            fields = parse_multipart(request.headers.get("content-type", ""), request.content)
            self.calls.append(ApiCall(method, fields))
            if fields.get("token") != self.token:
                return json_response({"ok": False, "error": "invalid_auth"})
            handler = self._api.get(method)
            n = self._api_counts.get(method, 0) + 1
            self._api_counts[method] = n
            if method == "conversations.info" and handler is None:
                ch = self.channels.get(fields.get("channel", ""))
                return json_response({"ok": True, "channel": ch} if ch else {"ok": False, "error": "channel_not_found"})
            if handler is None:
                return json_response({"ok": False, "error": "unknown_method"})
            result = handler(fields, n)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, httpx.Response):
                return result
            return json_response(result)
        if host == "files.slack.com":
            f = self.file_bodies.get(url.path)
            if f is None:
                return httpx.Response(404, text="missing")
            return httpx.Response(
                200,
                content=f.body,
                headers={"content-type": f.content_type, "content-length": str(len(f.body))},
            )
        raise RuntimeError(f"unexpected fetch {url}")

    # -- WebSocket ---------------------------------------------------------------------------

    async def create_web_socket(self, url: str, headers: dict[str, str]) -> FakeSocket:
        sock = FakeSocket(url, headers)
        self.sockets.append(sock)
        if self.auto_hello:
            sock.open()
            n = len(self.sockets)
            fast = self.hello_fast(n) if callable(self.hello_fast) else self.hello_fast
            sock.deliver({"type": "hello", "fast_reconnect": fast, "start": True})
        await sock.wait_open()
        return sock

    #: Alias matching the Python option name (``ws_factory=slack.ws_factory``).
    @property
    def ws_factory(self) -> Callable[[str, dict[str, str]], Awaitable[FakeSocket]]:
        return self.create_web_socket


def make_client(slack: FakeSlack, **extra: Any) -> Any:
    """Builds a ``GabrielClient`` wired to ``slack`` (Node: ``makeClient``).

    ``extra`` holds ``GabrielClient`` keyword options; ``advanced`` fields given as a mapping
    are merged into the test defaults (fake endpoints / transports, fast WebSocket backoff).
    """
    from gabriel.client.client import AdvancedOptions, GabrielClient, HydrationOptions
    from gabriel.core.ws import WsTimings

    advanced = AdvancedOptions(
        endpoints=ENDPOINTS,
        http_client=slack.http_client,
        ws_factory=slack.create_web_socket,
        start_timeout=5.0,
        ws_timings=WsTimings(backoff_base=0.01, backoff_cap=0.05),
        **(extra.pop("advanced", None) or {}),
    )
    options: dict[str, Any] = {
        "auth": AuthOptions(cookie_d=COOKIE),
        "hydration": HydrationOptions(hydrate_budget=0.2),
        **extra,
        "advanced": advanced,
    }
    return GabrielClient(**options)


async def tick(seconds: float = 0.0) -> None:
    """Sleeps ``seconds`` and then lets already-scheduled callbacks run."""
    await asyncio.sleep(seconds)
    for _ in range(5):
        await asyncio.sleep(0)


async def until(cond: Callable[[], bool], timeout: float = 3.0, label: str = "condition") -> None:
    start = time.monotonic()
    while not cond():
        if time.monotonic() - start > timeout:
            raise AssertionError(f"timed out waiting for {label}")
        await asyncio.sleep(0.005)


def message_frame(**over: Any) -> RawWsFrame:
    return {
        "type": "message",
        "channel": "C0GENERAL01",
        "user": "U0ALICE0001",
        "text": "hello",
        "ts": "1800000001.000100",
        "event_ts": "1800000001.000100",
        **over,
    }
