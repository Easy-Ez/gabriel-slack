"""Port of sdks/node/test/core.test.ts plus Python-specific transport/state-machine cases."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from gabriel.core.auth import AuthDead, AuthEvent, AuthOptions, AuthRefreshed, WorkspaceSession
from gabriel.core.core import GabrielCore, ProtocolDriftInfo
from gabriel.core.http import encode_multipart, is_safe_to_retry
from gabriel.core.types import RequestOpts
from gabriel.core.ws import FIRST_SEQ_ID, ReconnectingInfo, WsState, WsTimings
from gabriel.errors import GabrielError
from gabriel.util.aio import AbortController
from harness import COOKIE, ENDPOINTS, SELF, TEAM, TOKEN_1, FakeSlack, FileBody, json_response, tick, until

MakeCore = Callable[..., GabrielCore]


# -- auth + http -------------------------------------------------------------------------------


async def test_derives_the_workspace_session_from_the_cookie_alone(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    session = await core.initialize()
    assert session.team_id == TEAM
    assert session.api_base == "https://test.slack.com/"
    assert slack.auth_calls == 1


async def test_collapses_20_concurrent_invalid_auth_failures_into_one_exchange(
    slack: FakeSlack, make_core: MakeCore
) -> None:
    slack.on("auth.test", lambda _f, _n: {"ok": True})
    core = make_core(slack)
    await core.initialize()
    slack.token = "xoxc-rotated-2"
    results = await asyncio.gather(*(core.call_web_api("auth.test") for _ in range(20)))
    assert all(r["ok"] is True for r in results)
    assert slack.auth_calls == 2  # initial + exactly one refresh


async def test_enters_auth_dead_when_the_cookie_is_no_longer_signed_in(slack: FakeSlack, make_core: MakeCore) -> None:
    slack.on("auth.test", lambda _f, _n: {"ok": True})
    core = make_core(slack)
    await core.initialize()
    dead: list[str] = []
    core.on_auth_event(lambda e: dead.append(e.type))
    slack.token = "xoxc-rotated-3"
    slack.auth_alive = False
    with pytest.raises(GabrielError) as exc:
        await core.call_web_api("auth.test")
    assert exc.value.code == "AUTH_DEAD"
    assert core.auth_manager.state == "AUTH_DEAD"
    assert dead == ["dead"]
    with pytest.raises(GabrielError) as exc:
        await core.call_web_api("auth.test")
    assert exc.value.code == "AUTH_DEAD"


async def test_emits_refreshed_with_trigger_and_epoch_ms(slack: FakeSlack, make_core: MakeCore) -> None:
    slack.on("auth.test", lambda _f, _n: {"ok": True})
    core = make_core(slack)
    await core.initialize()
    events: list[AuthEvent] = []
    core.on_auth_event(events.append)
    slack.token = "xoxc-rotated-4"
    await core.call_web_api("auth.test")
    assert len(events) == 1
    evt = events[0]
    assert isinstance(evt, AuthRefreshed)
    assert evt.team_id == TEAM and evt.trigger == "invalid_auth"
    assert abs(evt.refreshed_at - time.time() * 1000) < 60_000
    assert core.session.token == "xoxc-rotated-4"


async def test_does_not_treat_an_outdated_user_agent_rejection_as_a_dead_session(
    slack: FakeSlack, make_core: MakeCore
) -> None:
    orig = slack.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "app.slack.test":
            return httpx.Response(403, text="Sorry, this browser is not supported")
        return await orig(request)

    slack.handle = handle  # type: ignore[method-assign]
    core = make_core(slack)
    with pytest.raises(GabrielError) as exc:
        await core.initialize()
    assert exc.value.code == "INVALID_STATE"
    assert core.auth_manager.state != "AUTH_DEAD"


async def test_shares_a_429_cool_down_per_method_and_retries(slack: FakeSlack, make_core: MakeCore) -> None:
    slack.on(
        "conversations.history",
        lambda _f, n: (
            json_response({"ok": False, "error": "ratelimited"}, 429, {"retry-after": "1"})
            if n == 1
            else {"ok": True, "messages": []}
        ),
    )
    core = make_core(slack)
    await core.initialize()
    started = time.monotonic()
    res = await core.call_web_api("conversations.history", {"channel": "C0GENERAL01"})
    assert res["ok"] is True
    assert time.monotonic() - started >= 0.9


async def test_429_cool_down_is_shared_by_concurrent_callers(slack: FakeSlack, make_core: MakeCore) -> None:
    slack.on(
        "conversations.history",
        lambda _f, n: (
            json_response({"ok": False, "error": "ratelimited"}, 429, {"retry-after": "1"})
            if n == 1
            else {"ok": True, "messages": []}
        ),
    )
    core = make_core(slack)
    await core.initialize()
    first = asyncio.ensure_future(core.call_web_api("conversations.history", {"channel": "C0GENERAL01"}))
    await until(lambda: len(slack.calls_to("conversations.history")) == 1)
    await tick(0.05)
    started = time.monotonic()
    await core.call_web_api("conversations.history", {"channel": "C0GENERAL01"})
    assert time.monotonic() - started >= 0.8  # the second caller waits out the same bucket
    await first


async def test_does_not_retry_non_idempotent_writes_on_server_errors(slack: FakeSlack, make_core: MakeCore) -> None:
    slack.on("chat.delete", lambda _f, _n: httpx.Response(502, text="oops"))
    core = make_core(slack)
    await core.initialize()
    with pytest.raises(GabrielError) as exc:
        await core.call_web_api("chat.delete", {"channel": "C0GENERAL01", "ts": "1.1"})
    assert exc.value.code == "SERVER_ERROR"
    assert len(slack.calls_to("chat.delete")) == 1


async def test_retries_read_only_methods_on_server_errors(slack: FakeSlack, make_core: MakeCore) -> None:
    slack.on(
        "conversations.history",
        lambda _f, n: httpx.Response(503, text="busy") if n == 1 else {"ok": True, "messages": []},
    )
    core = make_core(slack)
    await core.initialize()
    res = await core.call_web_api("conversations.history", {"channel": "C0GENERAL01"})
    assert res["ok"] is True
    assert len(slack.calls_to("conversations.history")) == 2


def test_retry_classification() -> None:
    assert is_safe_to_retry("conversations.history", {})
    assert is_safe_to_retry("users.info", {})
    assert is_safe_to_retry("reactions.add", {})
    assert is_safe_to_retry("chat.postMessage", {"client_msg_id": "cmid-1"})
    assert not is_safe_to_retry("chat.postMessage", {})
    assert not is_safe_to_retry("chat.delete", {})


async def test_rejects_a_workspace_value_that_would_redirect_credentials(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack, auth=AuthOptions(cookie_d=COOKIE, token=TOKEN_1, workspace="evil.example#"))
    with pytest.raises(GabrielError) as exc:
        await core.initialize()
    assert exc.value.code == "INVALID_ARGUMENT"


async def test_refuses_to_send_credentials_to_non_slack_hosts(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    await core.initialize()
    with pytest.raises(GabrielError) as exc:
        await core.http.fetch_authenticated("https://evil.example.com/x", method="GET")
    assert exc.value.code == "INVALID_ARGUMENT"
    with pytest.raises(GabrielError) as exc:
        await core.http.fetch_authenticated("http://files.slack.com/x")
    assert exc.value.code == "INVALID_ARGUMENT"


async def test_fetch_authenticated_sends_cookie_and_bearer_to_slack_hosts(
    slack: FakeSlack, make_core: MakeCore
) -> None:
    seen: list[httpx.Request] = []
    orig = slack.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return await orig(request)

    slack.handle = handle  # type: ignore[method-assign]
    slack.file_bodies["/files-pri/T0123456789-F0FILE00001/a.txt"] = FileBody(b"hi", "text/plain")
    core = make_core(slack)
    await core.initialize()
    resp = await core.http.fetch_authenticated("https://files.slack.com/files-pri/T0123456789-F0FILE00001/a.txt")
    assert resp.status_code == 200 and resp.content == b"hi"
    req = seen[-1]
    assert req.headers["cookie"] == f"d={COOKIE}"
    assert req.headers["authorization"] == f"Bearer {TOKEN_1}"
    await core.http.fetch_authenticated("https://files.slack.com/files-pri/T0123456789-F0FILE00001/a.txt", bearer=False)
    assert "authorization" not in seen[-1].headers


async def test_supplied_token_is_verified_with_auth_test(slack: FakeSlack, make_core: MakeCore) -> None:
    orig = slack.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "your-workspace.slack.com" and request.url.path == "/api/auth.test":
            ok = b"token=" + TOKEN_1.encode() in request.content
            return json_response({"ok": True, "team_id": TEAM, "user_id": SELF} if ok else {"ok": False})
        return await orig(request)

    slack.handle = handle  # type: ignore[method-assign]
    core = make_core(slack, auth=AuthOptions(cookie_d=COOKIE, token=TOKEN_1, workspace="your-workspace.slack.com"))
    session = await core.initialize()
    assert (session.team_id, session.user_id, session.domain) == (TEAM, SELF, "your-workspace")
    assert session.api_base == "https://your-workspace.slack.com/"
    assert slack.auth_calls == 0


async def test_allowed_teams_is_enforced(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack, auth=AuthOptions(cookie_d=COOKIE, allowed_teams=["T0OTHER0001"]))
    with pytest.raises(GabrielError) as exc:
        await core.initialize()
    assert exc.value.code == "PERMISSION_DENIED"


async def test_web_api_multipart_encoding_matches_the_web_client(slack: FakeSlack, make_core: MakeCore) -> None:
    captured: list[httpx.Request] = []
    orig = slack.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return await orig(request)

    slack.handle = handle  # type: ignore[method-assign]
    slack.on("chat.update", lambda f, _n: {"ok": True, "echo": f})
    core = make_core(slack)
    await core.initialize()
    res = await core.call_web_api(
        "chat.update",
        {"channel": "C0GENERAL01", "text": "a\nb", "blocks": [{"type": "divider"}], "flag": True, "n": 3, "skip": None},
        RequestOpts(reason="unit-test"),
    )
    req = captured[-1]
    assert req.method == "POST"
    query = parse_qs(urlsplit(str(req.url)).query)
    assert query["slack_route"] == [TEAM]
    assert query["_x_gantry"] == ["true"]
    assert "_x_id" in query and "_x_version_ts" in query
    assert req.headers["cookie"] == f"d={COOKIE}"
    assert req.headers["origin"] == "https://app.slack.com"
    assert req.headers["user-agent"].startswith("Mozilla/5.0")
    body = req.content.decode()
    assert "a\r\nb" in body  # CRLF on the wire
    assert list(res["echo"].keys()) == [
        "token",
        "channel",
        "text",
        "blocks",
        "flag",
        "n",
        "_x_reason",
        "_x_mode",
        "_x_sonic",
        "_x_app_name",
    ]
    echo = res["echo"]
    assert echo["blocks"] == '[{"type":"divider"}]'
    assert (echo["flag"], echo["n"], echo["text"], echo["_x_reason"]) == ("true", "3", "a\nb", "unit-test")
    assert (echo["_x_mode"], echo["_x_sonic"], echo["_x_app_name"]) == ("online", "true", "client")


def test_encode_multipart_format() -> None:
    body, ctype = encode_multipart({"a": "x\ny", 'q"n': "v"}, boundary="B")
    assert ctype == "multipart/form-data; boundary=B"
    assert body == (
        b'--B\r\nContent-Disposition: form-data; name="a"\r\n\r\nx\r\ny\r\n'
        b'--B\r\nContent-Disposition: form-data; name="q%22n"\r\n\r\nv\r\n--B--\r\n'
    )


async def test_edge_api_posts_json_with_token(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    await core.initialize()
    res = await core.call_edge_api("users/info", {"updated_ids": {"U0ALICE0001": 0}})
    assert res["results"][0]["id"] == "U0ALICE0001"
    assert slack.edge_calls[-1].endpoint == "users/info"
    assert slack.edge_calls[-1].body["token"] == TOKEN_1
    with pytest.raises(GabrielError) as exc:
        await core.call_edge_api("../evil")
    assert exc.value.code == "INVALID_ARGUMENT"


async def test_edge_api_refreshes_and_replays_on_invalid_auth(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    await core.initialize()
    slack.token = "xoxc-rotated-5"
    res = await core.call_edge_api("users/info", {"updated_ids": {"U0BOB000001": 0}})
    assert res["ok"] is True
    assert [c.body["token"] for c in slack.edge_calls] == [TOKEN_1, "xoxc-rotated-5"]


async def test_invalid_method_names_are_rejected(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    await core.initialize()
    with pytest.raises(GabrielError) as exc:
        await core.call_web_api("../../evil")
    assert exc.value.code == "INVALID_ARGUMENT"


async def test_maps_slack_errors(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    await core.initialize()
    with pytest.raises(GabrielError) as exc:
        await core.call_web_api("conversations.info", {"channel": "C0MISSING01"})
    assert exc.value.code == "CHANNEL_NOT_FOUND"
    assert exc.value.raw_slack_error == "channel_not_found"


async def test_request_signal_cancels(slack: FakeSlack, make_core: MakeCore) -> None:
    gate = asyncio.Event()

    async def slow(_f: dict[str, str], _n: int) -> dict[str, Any]:
        await gate.wait()
        return {"ok": True}

    slack.on("users.list", slow)
    core = make_core(slack)
    await core.initialize()
    ctl = AbortController()
    task = asyncio.ensure_future(core.call_web_api("users.list", {}, RequestOpts(signal=ctl.signal)))
    await tick(0.01)
    ctl.abort("stop")
    with pytest.raises(GabrielError) as exc:
        await task
    assert exc.value.code == "CANCELLED"
    gate.set()


async def test_request_timeout_maps_to_network_timeout(slack: FakeSlack, make_core: MakeCore) -> None:
    async def slow(_f: dict[str, str], _n: int) -> dict[str, Any]:
        await asyncio.sleep(1)
        return {"ok": True}

    slack.on("chat.delete", slow)
    core = make_core(slack)
    await core.initialize()
    with pytest.raises(GabrielError) as exc:
        await core.call_web_api("chat.delete", {}, RequestOpts(timeout=0.05))
    assert exc.value.code == "NETWORK_TIMEOUT"
    assert "timed out" in str(exc.value)


def test_session_repr_is_redacted() -> None:
    s = WorkspaceSession(
        team_id=TEAM, domain="test", api_base="https://test.slack.com/", user_id=SELF, token=TOKEN_1, cookie_d=COOKIE
    )
    text = repr(s) + repr(AuthOptions(cookie_d=COOKIE, token=TOKEN_1))
    assert TOKEN_1 not in text and COOKIE not in text
    assert "REDACTED" in text


async def test_auth_helpers(slack: FakeSlack, make_core: MakeCore) -> None:
    orig = slack.handle

    async def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "slack.test" and request.url.path == "/api/auth.findTeam":
            assert request.content == b"domain=your-workspace"
            assert "cookie" not in request.headers
            return json_response({"ok": True, "sso": False})
        return await orig(request)

    slack.handle = handle  # type: ignore[method-assign]
    core = make_core(slack)
    assert (await core.auth.find_team("https://your-workspace.slack.com/"))["ok"] is True
    with pytest.raises(GabrielError) as exc:
        await core.auth.find_team("evil.example/x")
    assert exc.value.code == "INVALID_ARGUMENT"
    result = await core.auth.exchange_cookie(COOKIE)
    assert [t.id for t in result.teams] == [TEAM] and result.lc == "1"
    assert TOKEN_1 not in repr(result)


# -- websocket lifecycle -----------------------------------------------------------------------


async def test_connects_with_cookie_headers_handshake_params_and_correlates_reply_to(
    slack: FakeSlack, make_core: MakeCore
) -> None:
    core = make_core(slack)
    await core.connect()
    await until(lambda: core.ws.state in ("RESYNCING", "READY"))
    sock = slack.socket
    assert sock.headers["Cookie"] == f"d={COOKIE}"
    assert sock.headers["Origin"] == "https://app.slack.com"
    params = parse_qs(urlsplit(sock.url).query)
    assert params["batch_presence_aware"] == ["1"]
    assert params["flannel"] == ["3"]
    assert params["gateway_server"] == [f"{TEAM}-1"]
    assert params["token"] == [TOKEN_1]
    assert params["start_args"][0].startswith("?agent=client&")
    p = asyncio.ensure_future(
        core.send_ws_command(
            {"type": "subscription_update", "subtype": "user_change_subscribe_request", "ids": ["U0ALICE0001"]}
        )
    )
    await until(lambda: len(sock.sent) == 1)
    sent = sock.sent[-1]
    assert sent["id"] == FIRST_SEQ_ID
    sock.deliver({"ok": True, "reply_to": sent["id"], "type": "subscription_update"})
    assert (await p)["reply_to"] == FIRST_SEQ_ID
    assert core.send_ws_frame({"type": "ping"}) == FIRST_SEQ_ID + 1


async def test_rejects_every_pending_command_immediately_when_the_socket_closes(
    slack: FakeSlack, make_core: MakeCore
) -> None:
    core = make_core(slack)
    await core.connect()
    await until(core.ws.is_connected)
    pending = [
        asyncio.ensure_future(core.send_ws_command({"type": "a"})),
        asyncio.ensure_future(core.send_ws_command({"type": "b"})),
    ]
    await tick()
    slack.socket.server_close(1006)
    for p in pending:
        with pytest.raises(GabrielError) as exc:
            await p
        assert exc.value.code == "SOCKET_CLOSED"


async def test_command_rejection_and_timeout(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    await core.connect()
    await until(core.ws.is_connected)
    p = asyncio.ensure_future(core.send_ws_command({"type": "x"}))
    await until(lambda: len(slack.socket.sent) == 1)
    slack.socket.deliver({"ok": False, "reply_to": slack.socket.sent[-1]["id"], "error": {"msg": "bad", "code": 2}})
    with pytest.raises(GabrielError) as exc:
        await p
    assert exc.value.code == "SLACK_API_ERROR" and exc.value.raw_slack_error == "bad"
    with pytest.raises(GabrielError) as exc:
        await core.send_ws_command({"type": "y"}, 0.02)
    assert exc.value.code == "NETWORK_TIMEOUT"


async def test_send_requires_a_connected_socket(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    with pytest.raises(GabrielError) as exc:
        core.send_ws_frame({"type": "ping"})
    assert exc.value.code == "SOCKET_CLOSED"


async def test_uses_the_reconnect_url_fast_path_after_a_drop_and_the_cold_path_after_a_fast_failure(
    slack: FakeSlack, make_core: MakeCore
) -> None:
    core = make_core(slack)
    await core.connect()
    await until(core.ws.is_connected)
    slack.socket.deliver({"type": "reconnect_url", "url": "wss://ws.slack.test/?frt=TICKET"})
    await tick()
    core.ws.mark_ready(False)
    slack.socket.server_close(1006)
    await until(lambda: len(slack.sockets) == 2)
    assert "frt=TICKET" in slack.sockets[1].url
    await until(core.ws.is_connected)
    core.ws.mark_ready(False)
    # Fast connection fails before hello → ticket discarded → next attempt is cold.
    slack.auto_hello = False
    slack.socket.server_close(1006)
    await until(lambda: len(slack.sockets) == 3)
    assert "frt=TICKET" in slack.sockets[2].url
    slack.sockets[2].server_close(1006)  # failed before hello
    await until(lambda: len(slack.sockets) == 4, 5.0)
    assert "frt=" not in slack.sockets[3].url


async def test_fast_ticket_expires_after_its_ttl(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack, ws_timings=WsTimings(fast_reconnect_ttl=0.0, backoff_base=0.01))
    await core.connect()
    await until(core.ws.is_connected)
    slack.socket.deliver({"type": "reconnect_url", "url": "wss://ws.slack.test/?frt=TICKET"})
    await tick(0.01)
    core.ws.mark_ready(False)
    slack.socket.server_close(1006)
    await until(lambda: len(slack.sockets) == 2)
    assert "frt=" not in slack.sockets[1].url


async def test_closes_with_4104_on_goodbye_and_reconnects(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    await core.connect()
    await until(core.ws.is_connected)
    core.ws.mark_ready(False)
    first = slack.socket
    first.deliver({"type": "goodbye"})
    await until(lambda: len(slack.sockets) == 2)
    await until(lambda: first.closed_with is not None)
    assert first.closed_with == 4104


async def test_reports_unknown_frame_types_once_as_protocol_drift(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    drift: list[str] = []
    core.on_protocol_drift(lambda d: drift.append(d.unknown_type))
    await core.connect()
    await until(core.ws.is_connected)
    slack.socket.deliver({"type": "brand_new_event"})
    slack.socket.deliver({"type": "brand_new_event"})
    await tick(0.005)
    assert drift == ["brand_new_event"]


async def test_dispatches_raw_type_and_subtype_handlers(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    raw: list[str | None] = []
    typed: list[str] = []
    plain: list[str] = []
    edits: list[str] = []
    hellos: list[bool] = []
    drift: list[ProtocolDriftInfo] = []
    core.on_raw_frame(lambda f: raw.append(f.get("type")))
    unsub = core.on_event("message", lambda f: typed.append(f["ts"]))
    core.on_message_subtype(None, lambda f: plain.append(f["ts"]))
    core.on_message_subtype("message_changed", lambda f: edits.append(f["ts"]))
    core.on_hello(lambda h: hellos.append(h.fast_reconnect))
    core.on_protocol_drift(drift.append)

    def boom(_f: dict[str, Any]) -> None:
        raise RuntimeError("handler bug")

    core.on_raw_frame(boom)  # a throwing handler must not break dispatch
    await core.connect()
    await until(core.ws.is_connected)
    slack.socket.deliver({"type": "message", "ts": "1.1"})
    slack.socket.deliver({"type": "message", "subtype": "message_changed", "ts": "1.2"})
    await tick(0.005)
    unsub()
    slack.socket.deliver({"type": "message", "ts": "1.3"})
    await tick(0.005)
    assert raw == ["hello", "message", "message", "message"]
    assert typed == ["1.1", "1.2"]
    assert plain == ["1.1", "1.3"]
    assert edits == ["1.2"]
    assert hellos == [False]
    assert drift == []


async def test_hello_timeout_reconnects(slack: FakeSlack, make_core: MakeCore) -> None:
    slack.auto_hello = False
    core = make_core(slack, ws_timings=WsTimings(hello_timeout=0.02, backoff_base=0.01))
    await core.connect()
    await until(lambda: len(slack.sockets) == 1)
    slack.sockets[0].open()
    await until(lambda: core.ws.state == "AWAITING_HELLO")
    await until(lambda: len(slack.sockets) == 2)
    assert slack.sockets[0].closed_with == 4000


async def test_pings_and_detects_a_dead_peer(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(
        slack,
        ws_timings=WsTimings(ping_interval=0.02, watchdog_interval=0.02, dead_peer_timeout=0.1, backoff_base=0.01),
    )
    await core.connect()
    await until(core.ws.is_connected)
    first = slack.socket
    await until(lambda: len(first.sent_of_type("ping")) >= 2)
    pings = first.sent_of_type("ping")
    assert pings[0]["id"] == FIRST_SEQ_ID and pings[1]["id"] == FIRST_SEQ_ID + 1
    # No downstream frames at all → dead peer → close + reconnect.
    await until(lambda: len(slack.sockets) == 2, 2.0)
    assert first.closed_with == 4000


async def test_error_frames(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    delays: list[float] = []
    core.ws.on("reconnecting", lambda info: delays.append(info.delay))
    await core.connect()
    await until(core.ws.is_connected)
    core.ws.mark_ready(False)
    # Ticket invalidation.
    slack.socket.deliver({"type": "reconnect_url", "url": "wss://ws.slack.test/?frt=TICKET"})
    slack.socket.deliver({"type": "error", "error": {"msg": "Socket URL has expired", "code": 1}})
    # Server busy: next attempt delayed by retry_after.
    slack.socket.deliver({"type": "error", "error": {"msg": "backend_overloaded", "retry_after": 0.05}})
    await tick(0.005)
    slack.socket.server_close(1006)
    await until(lambda: len(slack.sockets) == 2)
    assert delays == [0.05]
    assert "frt=" not in slack.sockets[1].url


async def test_invalid_auth_error_frame_refreshes_credentials(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    states: list[WsState] = []
    core.ws.on("state", lambda s, _p: states.append(s))
    await core.connect()
    await until(core.ws.is_connected)
    slack.token = "xoxc-rotated-6"
    slack.socket.deliver({"type": "error", "error": {"msg": "invalid_auth"}})
    await until(lambda: len(slack.sockets) == 2)
    assert "CREDENTIAL_REFRESH" in states
    assert parse_qs(urlsplit(slack.sockets[1].url).query)["token"] == ["xoxc-rotated-6"]
    assert slack.auth_calls == 2


async def test_handshake_401_with_dead_cookie_enters_auth_dead(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    dead: list[AuthEvent] = []
    core.on_auth_event(dead.append)
    await core.initialize()
    slack.auto_hello = False
    slack.auth_alive = False
    await core.connect()
    await until(lambda: len(slack.sockets) == 1)
    slack.socket.reject(401)
    await until(lambda: core.ws.state == "AUTH_DEAD")
    assert core.auth_manager.state == "AUTH_DEAD"
    assert len(dead) == 1 and isinstance(dead[0], AuthDead)
    await tick(0.05)
    assert len(slack.sockets) == 1  # terminal: no further attempts


async def test_handshake_429_honors_retry_after(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    infos: list[ReconnectingInfo] = []
    core.ws.on("reconnecting", infos.append)
    slack.auto_hello = False
    await core.connect()
    await until(lambda: len(slack.sockets) == 1)
    slack.auto_hello = True
    slack.socket.reject(429, {"Retry-After": "0.05"})
    await until(lambda: len(slack.sockets) == 2)
    assert infos[0].delay == 0.05 and infos[0].attempt == 1


async def test_stop_closes_with_1000_and_stops_reconnecting(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    closed: list[int] = []
    core.ws.on("closed", lambda info: closed.append(info.code))
    await core.connect()
    await until(core.ws.is_connected)
    sock = slack.socket
    core.disconnect()
    assert core.ws_state == "STOPPED"
    await until(lambda: sock.closed_with is not None)
    assert sock.closed_with == 1000
    await tick(0.05)
    assert len(slack.sockets) == 1
    assert closed == []  # client-initiated close is not reported as a drop


async def test_mark_ready_ignores_stale_epochs(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    hello_epochs: list[int] = []
    core.on_hello(lambda h: hello_epochs.append(h.epoch))
    await core.connect()
    await until(core.ws.is_connected)
    core.ws.mark_ready(False, hello_epochs[0] + 100)
    assert core.ws.state == "RESYNCING"
    core.ws.mark_ready(True, hello_epochs[0])
    assert core.ws.state == "READY_DEGRADED"
    assert core.ws.last_frame_age >= 0


async def test_non_json_frames_are_dropped(slack: FakeSlack, make_core: MakeCore) -> None:
    core = make_core(slack)
    frames: list[dict[str, Any]] = []
    core.on_raw_frame(frames.append)
    await core.connect()
    await until(core.ws.is_connected)
    slack.socket.deliver_raw("not json")
    slack.socket.deliver_raw(json.dumps([1, 2]))
    slack.socket.deliver({"type": "pong"})
    await tick(0.005)
    assert [f["type"] for f in frames] == ["hello", "pong"]


async def test_async_context_manager(slack: FakeSlack) -> None:
    async with GabrielCore(
        auth=AuthOptions(cookie_d=COOKIE),
        endpoints=ENDPOINTS,
        http_client=slack.http_client,
        ws_factory=slack.create_web_socket,
    ) as core:
        await until(core.ws.is_connected)
        sock = slack.socket
    assert core.ws_state == "STOPPED"
    assert sock.closed_with == 1000
    assert not slack.http_client.is_closed  # injected clients are not closed by the core


async def test_metadata_and_endpoint_overrides_accept_mappings(slack: FakeSlack) -> None:
    core = GabrielCore(
        auth=AuthOptions(cookie_d=COOKIE),
        metadata={"user_agent": "Mozilla/5.0 Test"},
        endpoints={"ws_base": "wss://ws.slack.test/"},
        http_client=slack.http_client,
    )
    assert core.metadata.user_agent == "Mozilla/5.0 Test"
    assert core.metadata.origin == "https://app.slack.com"
    assert core.endpoints.ws_base == "wss://ws.slack.test/"
    assert core.endpoints.edge_api_base == "https://edgeapi.slack.com/cache/"
    await core.close()


def test_cookie_is_required() -> None:
    with pytest.raises(GabrielError) as exc:
        GabrielCore(auth=AuthOptions(cookie_d=""))
    assert exc.value.code == "INVALID_ARGUMENT"
