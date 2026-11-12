"""GabrielClient against the in-memory fake Slack (port of sdks/node/test/client.test.ts)."""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest

from gabriel.client.chat import ChatSchedulerOptions
from gabriel.client.client import ConcurrencyOptions, GabrielClient, LoopGuardOptions
from gabriel.client.context import MessageMatcher
from gabriel.client.scope import ReadPolicy, ScopePolicy, WritePolicy
from gabriel.errors import GabrielError
from gabriel.events.types import (
    BaseEvent,
    ConnectionGapEvent,
    MessageCreatedEvent,
    PresenceChangedEvent,
    ReactionAddedEvent,
    UnreadBadgeCountsEvent,
)
from harness import SELF, FakeSlack, FileBody, make_client, message_frame, tick, until

Started = Callable[..., Awaitable[tuple[FakeSlack, GabrielClient]]]


@pytest.fixture
async def started() -> AsyncIterator[Started]:
    clients: list[GabrielClient] = []
    fakes: list[FakeSlack] = []

    async def factory(slack: FakeSlack | None = None, **extra: Any) -> tuple[FakeSlack, GabrielClient]:
        fake = slack or FakeSlack()
        fakes.append(fake)
        client = make_client(fake, **extra)
        clients.append(client)
        await client.start()
        return fake, client

    yield factory
    for c in clients:
        try:
            await c.stop(drain=0.1)
        except Exception:
            pass
    for f in fakes:
        await f.aclose()


async def expect_code(aw: Awaitable[Any], code: str) -> GabrielError:
    with pytest.raises(GabrielError) as info:
        await aw
    assert info.value.code == code
    return info.value


# ------------------------------------------------------------------------------------------- events


async def test_starts_from_cookie_and_emits_connection_ready() -> None:
    slack = FakeSlack()
    client = make_client(slack)
    ready: list[BaseEvent] = []
    client.on("connection.ready", lambda e, _ctx: ready.append(e))
    try:
        await client.start()
        await until(lambda: len(ready) == 1)
        assert client.self_user_id == SELF
        assert client.health().status == "healthy"
    finally:
        await client.stop(drain=0.1)
        await slack.aclose()


async def test_async_with_starts_and_stops() -> None:
    slack = FakeSlack()
    async with make_client(slack) as client:
        assert client.health().status == "healthy"
    assert slack.socket.closed_with is not None
    await slack.aclose()


async def test_hydrated_message_created_mentions_me_via_user_group(started: Started) -> None:
    slack, client = await started()
    got: list[MessageCreatedEvent] = []
    client.on(MessageCreatedEvent, lambda e, _ctx: got.append(e))
    slack.socket.deliver(message_frame(text="<!subteam^S0ONCALL001|@oncall> please look <@U0BOB000001>"))
    await until(lambda: len(got) == 1)
    evt = got[0]
    assert evt.sender.display_name == "Alice"
    assert evt.channel.name == "general"
    assert evt.markdown == "@oncall please look @Bob"
    assert evt.is_mentioning_me is True
    assert evt.mention_reasons == ["usergroup"]
    assert evt.mentions[0].to_dict() == {"kind": "usergroup", "id": "S0ONCALL001", "name": "oncall", "includesMe": True}
    assert '<slack_message untrusted="true">' in evt.to_llm_context()
    assert "toLLMContext" not in json.loads(evt.to_json())


async def test_reports_user_group_membership_only_once_known(started: Started) -> None:
    slack = FakeSlack()
    slack.on("usergroups.list", lambda _f, _n: {"ok": False, "error": "missing_scope"})
    _, client = await started(slack)
    got: list[MessageCreatedEvent] = []
    client.on(MessageCreatedEvent, lambda e, _ctx: got.append(e))

    def mention(ts: str) -> None:
        slack.socket.deliver(message_frame(text="<!subteam^S0ONCALL001> look", ts=ts, event_ts=ts))

    mention("1800000001.000001")
    await until(lambda: len(got) == 1)
    assert got[0].mentions[0].includes_me is None
    assert got[0].mention_reasons == []
    assert got[0].is_mentioning_me is False

    slack.socket.deliver({"type": "subteam_self_added", "subteam_id": "S0ONCALL001"})
    mention("1800000001.000002")
    await until(lambda: len(got) == 2)
    assert got[1].mentions[0].includes_me is True
    assert got[1].mention_reasons == ["usergroup"]

    slack.socket.deliver({"type": "subteam_self_removed", "subteam_id": "S0ONCALL001"})
    mention("1800000001.000003")
    await until(lambda: len(got) == 3)
    assert got[2].mentions[0].includes_me is False
    assert got[2].is_mentioning_me is False


async def test_marks_preloaded_groups_without_me_as_includes_me_false(started: Started) -> None:
    slack = FakeSlack()
    slack.on(
        "usergroups.list",
        lambda _f, _n: {
            "ok": True,
            "usergroups": [
                {"id": "S0ONCALL001", "handle": "oncall", "users": ["U0BOB000001"]},
                {"id": "S0DESIGN001", "handle": "design"},
            ],
        },
    )
    _, client = await started(slack)
    got: list[MessageCreatedEvent] = []
    client.on(MessageCreatedEvent, lambda e, _ctx: got.append(e))
    slack.socket.deliver(message_frame(text="<!subteam^S0ONCALL001> <!subteam^S0DESIGN001> <!here>"))
    await until(lambda: len(got) == 1)
    assert [m.includes_me for m in got[0].mentions] == [False, None, None]
    assert got[0].mention_reasons == ["broadcast"]
    assert got[0].is_mentioning_me is True


async def test_per_channel_order_even_when_hydration_finishes_out_of_order(started: Started) -> None:
    slack = FakeSlack()
    slack.users["U0SLOW00001"] = {"id": "U0SLOW00001", "name": "slow", "profile": {"display_name": "Slow"}}
    original = slack.handle

    async def slow_handle(request: httpx.Request) -> httpx.Response:
        if b"U0SLOW00001" in (request.content or b""):
            await tick(0.12)
        return await original(request)

    slack.handle = slow_handle  # type: ignore[method-assign]
    _, client = await started(slack)
    order: list[str] = []
    client.on("message.created", lambda e, _ctx: order.append(e.ts))
    slack.socket.deliver(message_frame(user="U0SLOW00001", ts="1800000001.000001"))
    slack.socket.deliver(message_frame(ts="1800000001.000002"))
    slack.socket.deliver(message_frame(ts="1800000001.000003"))
    await until(lambda: len(order) == 3)
    assert order == ["1800000001.000001", "1800000001.000002", "1800000001.000003"]


async def test_renders_names_that_arrive_after_the_budget_but_before_the_handler(started: Started) -> None:
    slack = FakeSlack()
    slack.users["U0SLOW00001"] = {"id": "U0SLOW00001", "name": "slow", "profile": {"display_name": "Slow"}}
    original = slack.handle

    async def slow_handle(request: httpx.Request) -> httpx.Response:
        if b"U0SLOW00001" in (request.content or b""):
            await tick(0.3)  # budget is 0.2 s
        return await original(request)

    slack.handle = slow_handle  # type: ignore[method-assign]
    _, client = await started(slack)
    names: list[str | None] = []

    async def handler(e: Any, _ctx: Any) -> None:
        if not names:
            await tick(0.5)  # keeps the second message queued behind this one
        names.append(e.sender.display_name)

    client.on("message.created", handler)
    slack.socket.deliver(message_frame(ts="1800000001.000001"))
    slack.socket.deliver(message_frame(user="U0SLOW00001", ts="1800000001.000002"))
    await until(lambda: len(names) == 2)
    assert names[1] == "Slow"


async def test_dedupes_redelivered_frames_by_event_id(started: Started) -> None:
    slack, client = await started()
    n = 0

    def count(_e: BaseEvent, _ctx: Any) -> None:
        nonlocal n
        n += 1

    client.on("message.created", count)
    slack.socket.deliver(message_frame())
    slack.socket.deliver(message_frame())
    await tick(0.08)
    assert n == 1


async def test_filters_own_echoes_but_not_messages_typed_elsewhere(started: Started) -> None:
    slack, client = await started()
    origins: list[str] = []
    client.on("message.created", lambda e, _ctx: origins.append(e.origin))
    res = await client.chat.send("C0GENERAL01", "from sdk")
    cmid = slack.calls_to("chat.postMessage")[0].fields["client_msg_id"]
    slack.socket.deliver(message_frame(user=SELF, ts=res.ts, client_msg_id=cmid, text="from sdk"))
    slack.socket.deliver(message_frame(user=SELF, ts="1800000009.000001", text="typed in browser"))
    await until(lambda: len(origins) == 1)
    await tick(0.05)
    assert origins == ["self_other_client"]


async def test_message_other_and_badge_snapshots_once(started: Started) -> None:
    slack, client = await started()
    types: list[str] = []
    client.on("message.other", lambda e, _ctx: types.append(e.type))

    def on_badge(e: UnreadBadgeCountsEvent, _ctx: Any) -> None:
        types.append(f"{e.type}:{e.mention_count}")

    client.on(UnreadBadgeCountsEvent, on_badge)
    slack.socket.deliver(message_frame(subtype="channel_join"))
    badge = {"type": "badge_counts_updated", "activity_v2": {"at_user": 1, "thread_v2": 2}, "event_ts": "1"}
    slack.socket.deliver(badge)
    slack.socket.deliver({**badge, "event_ts": "2"})
    await until(lambda: len(types) == 2)
    await tick(0.03)
    # Different lanes: no cross-lane ordering guarantee.
    assert sorted(types) == ["message.other", "unread.badge_counts:1"]


async def test_enriches_reaction_targets_with_thread_root(started: Started) -> None:
    slack = FakeSlack()
    slack.on(
        "conversations.replies",
        lambda _f, _n: {"ok": True, "messages": [{"ts": "1800000000.000100", "thread_ts": "1800000000.000100"}]},
    )
    _, client = await started(slack)
    got: list[tuple[str | None, str | None]] = []
    client.on(ReactionAddedEvent, lambda e, _ctx: got.append((e.target.ts, e.target.thread_root_ts)))
    slack.socket.deliver(
        {
            "type": "reaction_added",
            "user": SELF,
            "reaction": "memo",
            "item": {"type": "message", "channel": "C0GENERAL01", "ts": "1800000002.000100"},
            "event_ts": "1800000003.000000",
        }
    )
    await until(lambda: len(got) == 1)
    assert got[0] == ("1800000002.000100", "1800000000.000100")


async def test_isolates_handler_errors_and_routes_them_to_on_error(started: Started) -> None:
    slack, client = await started()
    errors: list[BaseException] = []
    after = 0
    client.on_error(lambda err, _evt: errors.append(err))

    def handler(e: MessageCreatedEvent, _ctx: Any) -> None:
        nonlocal after
        if e.ts == "1800000001.000001":
            raise RuntimeError("boom")
        after += 1

    client.on("message.created", handler)
    slack.socket.deliver(message_frame(ts="1800000001.000001"))
    slack.socket.deliver(message_frame(ts="1800000001.000002"))
    await until(lambda: after == 1)
    assert "boom" in str(errors[0])


async def test_aborts_ctx_signal_when_trigger_message_deleted(started: Started) -> None:
    slack, client = await started()
    aborted = False

    async def handler(_e: BaseEvent, ctx: Any) -> None:
        nonlocal aborted
        await ctx.signal.wait()
        aborted = True

    client.on("message.created", handler)
    slack.socket.deliver(message_frame())
    await tick(0.05)
    slack.socket.deliver(
        {
            "type": "message",
            "subtype": "message_deleted",
            "channel": "C0GENERAL01",
            "deleted_ts": "1800000001.000100",
            "ts": "1800000099.000000",
        }
    )
    await until(lambda: aborted)


async def test_pull_mode_iteration(started: Started) -> None:
    slack, client = await started()
    it = client.events()
    slack.socket.deliver(message_frame())
    async with asyncio.timeout(3):
        async for evt in it:
            if evt.type == "message.created":
                break
    await it.aclose()


async def test_decorator_registration_and_matchers(started: Started) -> None:
    slack, client = await started()
    seen: list[str] = []

    @client.on_message(mentions_me=True)
    async def mentioned(evt: MessageCreatedEvent, _ctx: Any) -> None:
        seen.append(f"mention:{evt.ts}")

    @client.on_message(MessageMatcher(pattern=r"^deploy"))
    def deploy(evt: MessageCreatedEvent, _ctx: Any) -> None:
        seen.append(f"deploy:{evt.ts}")

    @client.on_thread_reply()
    def reply(evt: MessageCreatedEvent, _ctx: Any) -> None:
        seen.append(f"reply:{evt.ts}")

    slack.socket.deliver(message_frame(ts="1800000001.000001", text=f"<@{SELF}> hi"))
    slack.socket.deliver(message_frame(ts="1800000001.000002", text="deploy now"))
    slack.socket.deliver(message_frame(ts="1800000001.000003", text="ok", thread_ts="1800000001.000002"))
    await until(lambda: len(seen) == 3)
    assert seen == ["mention:1800000001.000001", "deploy:1800000001.000002", "reply:1800000001.000003"]


async def test_middleware_wraps_handlers(started: Started) -> None:
    slack, client = await started()
    log: list[str] = []

    async def mw(evt: BaseEvent, _ctx: Any, nxt: Callable[[], Awaitable[None]]) -> None:
        log.append(f"before:{evt.type}")
        await nxt()
        log.append("after")

    client.use(mw)
    client.on("message.created", lambda _e, _ctx: log.append("handler"))
    slack.socket.deliver(message_frame())
    await until(lambda: "after" in log)
    assert log == ["before:message.created", "handler", "after"]


# ------------------------------------------------------------------------------- handler timeouts


async def test_times_out_slow_handlers(started: Started) -> None:
    slack, client = await started(concurrency=ConcurrencyOptions(handler_timeout=0.05))
    codes: list[str] = []
    handled: list[str] = []
    client.on_error(lambda err, _evt: codes.append(getattr(err, "code", "")))

    async def handler(evt: MessageCreatedEvent, ctx: Any) -> None:
        if evt.ts == "1800000001.000001":
            await ctx.signal.wait()
        handled.append(evt.ts)

    client.on("message.created", handler)
    slack.socket.deliver(message_frame(ts="1800000001.000001"))
    slack.socket.deliver(message_frame(ts="1800000001.000002"))
    await until(lambda: len(handled) == 2)
    assert codes == ["HANDLER_TIMEOUT"]
    assert handled == ["1800000001.000001", "1800000001.000002"]


# ------------------------------------------------------------------------------------ ctx helpers


async def test_reply_in_thread_and_wait_for_follow_up_without_deadlock(started: Started) -> None:
    slack, client = await started()
    answer: list[str] = []
    other_handled = False

    async def handler(evt: MessageCreatedEvent, ctx: Any) -> None:
        nonlocal other_handled
        if evt.markdown == "!deploy":
            await ctx.reply("confirm?")
            reply = await ctx.wait_for_reply(
                lambda r: r.thread_ts == evt.ts and re.fullmatch(r"yes|no", r.markdown) is not None, timeout=2.0
            )
            answer.append(reply.markdown if reply else "")
        elif evt.markdown == "unrelated":
            other_handled = True

    client.on("message.created", handler)
    slack.socket.deliver(message_frame(text="!deploy", ts="1800000001.000100"))
    await until(lambda: len(slack.calls_to("chat.postMessage")) == 1)
    assert slack.calls_to("chat.postMessage")[0].fields["thread_ts"] == "1800000001.000100"
    slack.socket.deliver(message_frame(text="unrelated", ts="1800000001.000200"))
    await until(lambda: other_handled)
    slack.socket.deliver(message_frame(text="yes", ts="1800000001.000300", thread_ts="1800000001.000100"))
    await until(lambda: len(answer) == 1)
    assert answer == ["yes"]


async def test_loop_breaker_trips_for_automatic_replies(started: Started) -> None:
    slack, client = await started(loop_guard=LoopGuardOptions(max_auto_replies_per_minute=2))
    codes: list[str] = []
    loops = 0

    def on_loop(_e: BaseEvent, _ctx: Any) -> None:
        nonlocal loops
        loops += 1

    client.on("guard.loop_detected", on_loop)

    async def handler(_e: BaseEvent, ctx: Any) -> None:
        try:
            await ctx.reply("auto")
        except GabrielError as err:
            codes.append(err.code)

    client.on("message.created", handler)
    for i in range(1, 4):
        slack.socket.deliver(message_frame(ts=f"1800000001.00000{i}", thread_ts="1800000000.000001"))
    await until(lambda: len(codes) == 1)
    assert codes == ["LOOP_DETECTED"]
    assert loops == 1
    assert len(slack.calls_to("chat.postMessage")) == 2


# ------------------------------------------------------------------------------------------- chat


async def test_auto_chunks_oversized_markdown_fifo_with_unique_client_msg_id(started: Started) -> None:
    slack, client = await started()
    para = "lorem ipsum " * 500
    res = await client.chat.send("C0GENERAL01", "\n\n".join([para] * 6))
    calls = slack.calls_to("chat.postMessage")
    assert len(calls) > 1
    assert len(res.chunk_ts) == len(calls)
    assert len({c.fields["client_msg_id"] for c in calls}) == len(calls)
    for c in calls:
        assert len(c.fields["markdown_text"]) <= 11_500


async def test_resplits_at_0_8x_on_msg_too_long(started: Started) -> None:
    slack = FakeSlack()
    first = True

    def post(f: dict[str, str], _n: int) -> dict[str, Any]:
        nonlocal first
        if first:
            first = False
            return {"ok": False, "error": "msg_too_long"}
        return {"ok": True, "ts": f"{1800000000 + random.randrange(1_000_000)}.000100", "channel": f["channel"]}

    slack.on("chat.postMessage", post)
    _, client = await started(slack)
    res = await client.chat.send("C0GENERAL01", "x " * 5000)
    assert len(res.chunk_ts) > 1


async def test_restores_unread_cursor_after_posting(started: Started) -> None:
    slack = FakeSlack()
    slack.on(
        "conversations.info",
        lambda _f, _n: {
            "ok": True,
            "channel": {"id": "C0GENERAL01", "last_read": "1800000000.000001", "unread_count": 3},
        },
    )
    slack.on("conversations.mark", lambda _f, _n: {"ok": True})
    _, client = await started(slack)
    await asyncio.gather(
        client.chat.send("C0GENERAL01", "a", preserve_unread_cursor=True),
        client.chat.send("C0GENERAL01", "b", preserve_unread_cursor=True),
    )
    marks = slack.calls_to("conversations.mark")
    assert len(marks) == 1
    assert marks[0].fields["ts"] == "1800000000.000001"


async def test_streams_with_single_inflight_update_and_final_content(started: Started) -> None:
    slack = FakeSlack()
    updates: list[str] = []

    async def update(f: dict[str, str], _n: int) -> dict[str, Any]:
        updates.append(f["markdown_text"])
        await tick(0.03)
        return {"ok": True, "ts": f["ts"], "channel": f["channel"]}

    slack.on("chat.update", update)
    _, client = await started(slack, chat=ChatSchedulerOptions(stream_updates_per_second=100, stream_min_interval=0))
    stream = await client.chat.create_stream("C0GENERAL01", initial_markdown="thinking")
    for i in range(50):
        stream.append(f"{i} ")
        await tick(0.001)
    stream.append("```js\nconst x = 1;")
    done = await stream.finish()
    assert done.ts == stream.ts
    assert len(updates) < 50
    assert "49 ```js\nconst x = 1;" in updates[-1]
    with pytest.raises(GabrielError, match="already finished"):
        stream.append("late")


async def test_stream_context_manager(started: Started) -> None:
    slack, client = await started(chat=ChatSchedulerOptions(stream_updates_per_second=100, stream_min_interval=0))
    async with client.chat.stream("C0GENERAL01", thread_ts="1800000000.000100") as s:
        s.append("hello ")
        s.append("world")
    updates = slack.calls_to("chat.update")
    assert updates[-1].fields["markdown_text"] == "hello world"
    assert slack.calls_to("chat.postMessage")[0].fields["thread_ts"] == "1800000000.000100"


async def test_continues_stream_in_new_message_past_budget(started: Started) -> None:
    slack = FakeSlack()
    finals: dict[str, str] = {}

    def update(f: dict[str, str], _n: int) -> dict[str, Any]:
        finals[f["ts"]] = f["markdown_text"]
        return {"ok": True, "ts": f["ts"], "channel": f["channel"]}

    slack.on("chat.update", update)
    _, client = await started(slack, chat=ChatSchedulerOptions(stream_updates_per_second=1000, stream_min_interval=0))
    stream = await client.chat.create_stream("C0GENERAL01", thread_ts="1800000000.000100")
    paragraph = ("token " * 300).strip()
    expected = ""
    for i in range(12):
        chunk = f"{paragraph} {i}\n\n"
        expected += chunk
        stream.append(chunk)
    done = await stream.finish()
    assert len(done.continuation_ts) >= 1
    posts = slack.calls_to("chat.postMessage")
    assert all(p.fields.get("thread_ts") == "1800000000.000100" for p in posts[1:])
    joined = "\n\n".join(finals.get(ts, "") for ts in [done.ts, *done.continuation_ts])
    assert re.sub(r"\s+", " ", joined).strip() == re.sub(r"\s+", " ", expected).strip()
    for text in finals.values():
        assert len(text) <= 11_500


async def test_keeps_text_appended_during_continuation_post(started: Started) -> None:
    slack = FakeSlack()
    finals: dict[str, str] = {}

    def update(f: dict[str, str], _n: int) -> dict[str, Any]:
        finals[f["ts"]] = f["markdown_text"]
        return {"ok": True, "ts": f["ts"], "channel": f["channel"]}

    slack.on("chat.update", update)
    posts = 0
    on_second_post: list[Callable[[], None]] = []

    async def post(f: dict[str, str], _n: int) -> dict[str, Any]:
        nonlocal posts
        posts += 1
        if posts == 2:
            for cb in on_second_post:
                cb()
            await tick(0.04)
        ts = f"{1800000100 + posts}.000100"
        if posts > 1:
            finals[ts] = f["markdown_text"]
        return {"ok": True, "ts": ts, "channel": f["channel"]}

    slack.on("chat.postMessage", post)
    _, client = await started(slack, chat=ChatSchedulerOptions(stream_updates_per_second=1000, stream_min_interval=0))
    stream = await client.chat.create_stream("C0GENERAL01")
    on_second_post.append(lambda: stream.append(" TAIL-APPENDED-DURING-ROLLOVER"))
    stream.append(f"{'word ' * 2600}\n\n{'more ' * 400}")
    await until(lambda: posts >= 2)
    await tick(0.08)
    done = await stream.finish()
    joined = " ".join(finals.get(ts, "") for ts in [done.ts, *done.continuation_ts])
    assert "TAIL-APPENDED-DURING-ROLLOVER" in joined


async def test_aborts_stream_when_its_message_is_deleted(started: Started) -> None:
    slack, client = await started()
    stream = await client.chat.create_stream("C0GENERAL01")
    slack.socket.deliver(
        {
            "type": "message",
            "subtype": "message_deleted",
            "channel": "C0GENERAL01",
            "deleted_ts": stream.ts,
            "ts": "1800009999.000000",
        }
    )
    await until(lambda: stream.signal.aborted)


async def test_retries_message_not_found_within_creation_window(started: Started) -> None:
    slack = FakeSlack()
    slack.on(
        "chat.update",
        lambda f, n: {"ok": False, "error": "message_not_found"} if n < 3 else {"ok": True, "ts": f["ts"]},
    )
    _, client = await started(slack)
    res = await client.chat.send("C0GENERAL01", "x")
    await client.chat.update("C0GENERAL01", res.ts, "y")
    assert len(slack.calls_to("chat.update")) == 3


# --------------------------------------------------------------------------------------- presence


async def test_presence_full_set_initial_snapshots_and_reconnect_dedupe(started: Started) -> None:
    slack, client = await started()
    events: list[tuple[str, str, bool]] = []

    def on_presence(e: PresenceChangedEvent, _ctx: Any) -> None:
        events.append((e.user.id, e.presence, e.is_initial_snapshot))

    client.on(PresenceChangedEvent, on_presence)
    await client.presence.watch(["U0ALICE0001"])
    await client.presence.watch(["U0BOB000001"])
    await until(lambda: len(slack.socket.sent_of_type("presence_sub")) >= 1)
    await tick(0.15)
    subs = slack.socket.sent_of_type("presence_sub")
    assert subs[-1]["ids"] == ["U0ALICE0001", "U0BOB000001"]
    slack.socket.deliver({"type": "presence_change", "presence": "active", "users": ["U0ALICE0001", "U0BOB000001"]})
    await until(lambda: len(events) == 2)
    assert all(initial for _, _, initial in events)

    # Reconnect: snapshot replays must not re-emit unchanged users.
    client.core.ws.mark_ready(False)
    slack.socket.server_close(1006)
    await until(lambda: len(slack.sockets) == 2 and len(slack.socket.sent_of_type("presence_sub")) == 1, timeout=5.0)
    slack.socket.deliver({"type": "presence_change", "presence": "active", "users": ["U0ALICE0001"]})
    slack.socket.deliver({"type": "presence_change", "presence": "away", "users": ["U0BOB000001"]})
    await until(lambda: len(events) == 3)
    await tick(0.03)
    assert events[2] == ("U0BOB000001", "away", False)


async def test_enforces_presence_frame_cap(started: Started) -> None:
    _, client = await started()
    ids = [f"U{str(i).zfill(10)}" for i in range(1300)]
    await expect_code(client.presence.watch(ids), "SUBSCRIPTION_CAP_EXCEEDED")


async def test_unsubscribes_when_last_handle_released(started: Started) -> None:
    slack, client = await started()
    a = await client.presence.watch(["U0ALICE0001"])
    b = await client.presence.watch(["U0ALICE0001"])
    await tick(0.15)
    await a.unsubscribe()
    await tick(0.15)
    assert slack.socket.sent_of_type("presence_sub")[-1]["ids"] == ["U0ALICE0001"]
    async with b:
        pass
    await tick(0.15)
    assert slack.socket.sent_of_type("presence_sub")[-1]["ids"] == []


# ---------------------------------------------------------------------------------- scope sandbox


async def test_scope_allows_trigger_thread_writes_and_rejects_everything_else(started: Started) -> None:
    slack, client = await started()
    agent = client.scope(
        ScopePolicy(
            read=ReadPolicy(channels=["C0GENERAL01"], allow_search=False),
            write=WritePolicy(
                target_scope="trigger_thread_only",
                allowed_actions=["chat.send", "reactions.add"],
                disclosure_suffix="\n_ai_",
            ),
        )
    )
    await expect_code(agent.chat.send("C0GENERAL01", "outside handler"), "SCOPE_POLICY_VIOLATION")
    with pytest.raises(GabrielError, match="not available"):
        _ = agent.core
    results: list[str] = []

    async def handler(evt: MessageCreatedEvent, _ctx: Any) -> None:
        async def try_it(label: str, aw: Awaitable[Any]) -> None:
            try:
                await aw
                results.append(f"{label}:ok")
            except GabrielError as err:
                results.append(f"{label}:{err.code}")

        await try_it("thread", agent.chat.send(evt.channel.id, "answer", thread_ts=evt.ts))
        await try_it("top-level", agent.chat.send(evt.channel.id, "x"))
        await try_it("other-channel", agent.chat.send("C0RANDOM001", "x", thread_ts=evt.ts))
        await try_it("delete", agent.chat.delete(evt.channel.id, evt.ts))
        await try_it("search", agent.search.messages("secret"))
        await try_it("history", agent.channels.get_history("C0RANDOM001"))
        await try_it("react", agent.reactions.add(evt.channel.id, evt.ts, "eyes"))

    client.on("message.created", handler)
    slack.socket.deliver(message_frame())
    await until(lambda: len(results) == 7)
    assert results == [
        "thread:ok",
        "top-level:SCOPE_POLICY_VIOLATION",
        "other-channel:SCOPE_POLICY_VIOLATION",
        "delete:SCOPE_POLICY_VIOLATION",
        "search:SCOPE_POLICY_VIOLATION",
        "history:SCOPE_POLICY_VIOLATION",
        "react:ok",
    ]
    assert slack.calls_to("chat.postMessage")[0].fields["markdown_text"] == "answer\n_ai_"


async def test_scope_maps_download_overruns_to_policy_violation(started: Started) -> None:
    slack = FakeSlack()
    slack.file_bodies["/files-pri/T-F1/big.log"] = FileBody(bytes(4096), "text/plain")
    _, client = await started(slack)
    agent = client.scope(ScopePolicy(read=ReadPolicy(max_file_download_bytes=1024)))

    class Ref:
        url_private = "https://files.slack.com/files-pri/T-F1/big.log"

    await expect_code(agent.files.download_bytes(Ref()), "SCOPE_POLICY_VIOLATION")
    await expect_code(client.files.download_bytes(Ref(), max_bytes=100), "FILE_TOO_LARGE")
    ok = await client.files.download_bytes(Ref())
    assert len(ok) == 4096


async def test_scope_approval_hook_is_consulted(started: Started) -> None:
    slack, client = await started()
    asked: list[str] = []

    async def approval(action: str, _params: dict[str, Any]) -> bool:
        asked.append(action)
        return action == "reactions.add"

    agent = client.scope(ScopePolicy(approval=approval))
    await agent.reactions.add("C0GENERAL01", "1800000001.000100", ":eyes:")
    await expect_code(agent.chat.send("C0GENERAL01", "hi"), "SCOPE_POLICY_VIOLATION")
    assert asked == ["reactions.add", "chat.send"]
    assert slack.calls_to("reactions.add")[0].fields["name"] == "eyes"


# ----------------------------------------------------------------------------------- gap recovery


async def test_backfills_history_after_cold_reconnect_before_buffered_realtime(started: Started) -> None:
    slack = FakeSlack()
    history_gate: asyncio.Event = asyncio.Event()
    slack.on(
        "client.counts",
        lambda _f, _n: {
            "ok": True,
            "channels": [{"id": "C0GENERAL01", "latest": "1800000005.000100"}],
            "mpims": [],
            "ims": [],
        },
    )

    async def history(_f: dict[str, str], _n: int) -> dict[str, Any]:
        await history_gate.wait()
        return {
            "ok": True,
            "has_more": False,
            "messages": [
                {"type": "message", "user": "U0BOB000001", "text": "missed 2", "ts": "1800000003.000100"},
                {"type": "message", "user": "U0BOB000001", "text": "missed 1", "ts": "1800000002.000100"},
            ],
        }

    slack.on("conversations.history", history)
    _, client = await started(slack)
    seen: list[tuple[str, str]] = []
    gaps: list[ConnectionGapEvent] = []
    client.on("message.created", lambda e, _ctx: seen.append((e.ts, e.delivery)))
    client.on(ConnectionGapEvent, lambda e, _ctx: gaps.append(e))
    slack.socket.deliver(message_frame(ts="1800000001.000100"))
    await until(lambda: len(seen) == 1)
    await tick(0.02)

    client.core.ws.mark_ready(False)
    slack.socket.server_close(1006)  # no frt ticket → cold reconnect
    await until(lambda: len(slack.sockets) == 2, timeout=5.0)
    await until(lambda: len(slack.calls_to("conversations.history")) == 1)
    slack.socket.deliver(message_frame(ts="1800000004.000100", text="realtime during resync"))
    await tick(0.02)
    history_gate.set()
    await until(lambda: len(seen) == 4)
    assert seen == [
        ("1800000001.000100", "realtime"),
        ("1800000002.000100", "backfill"),
        ("1800000003.000100", "backfill"),
        ("1800000004.000100", "realtime"),
    ]
    assert slack.calls_to("conversations.history")[0].fields["oldest"] == "1800000001.000100"
    await until(lambda: len(gaps) == 1)
    gap = gaps[0].to_dict()
    assert gap["cause"] == "cold_resync"
    assert gap["recovered"] is True
    assert gap["recoveredCount"] == 2
    assert gap["channelsRecovered"] == ["C0GENERAL01"]


async def test_resolves_names_of_a_whole_backfill_before_dispatching_it(started: Started) -> None:
    slack = FakeSlack()
    slack.users["U0SLOW00001"] = {"id": "U0SLOW00001", "name": "slow", "profile": {"display_name": "Slow"}}
    slack.on(
        "client.counts",
        lambda _f, _n: {
            "ok": True,
            "channels": [{"id": "C0GENERAL01", "latest": "1800000005.000100"}],
            "mpims": [],
            "ims": [],
        },
    )
    slack.on(
        "conversations.history",
        lambda _f, _n: {
            "ok": True,
            "has_more": False,
            "messages": [
                {"type": "message", "user": "U0SLOW00001", "text": "missed 2", "ts": "1800000003.000100"},
                {"type": "message", "user": "U0SLOW00001", "text": "missed 1", "ts": "1800000002.000100"},
            ],
        },
    )
    original = slack.handle

    async def slow_handle(request: httpx.Request) -> httpx.Response:
        if b"U0SLOW00001" in (request.content or b""):
            await tick(0.4)  # per-event budget is 0.2 s
        return await original(request)

    slack.handle = slow_handle  # type: ignore[method-assign]
    _, client = await started(slack)
    seen: list[tuple[str, str | None]] = []
    client.on("message.created", lambda e, _ctx: seen.append((e.delivery, e.sender.display_name)))
    slack.socket.deliver(message_frame(ts="1800000001.000100"))
    await until(lambda: len(seen) == 1)
    await tick(0.02)

    client.core.ws.mark_ready(False)
    slack.socket.server_close(1006)  # no frt ticket → cold reconnect
    await until(lambda: len(seen) == 3, timeout=5.0)
    assert seen[1:] == [("backfill", "Slow"), ("backfill", "Slow")]


# -------------------------------------------------------------------------------------- lifecycle


async def test_reports_dead_auth(started: Started) -> None:
    slack, client = await started()
    dead: list[BaseEvent] = []
    client.on("auth.dead", lambda e, _ctx: dead.append(e))
    slack.token = "xoxc-rotated"
    slack.auth_alive = False
    await expect_code(client.users.get_dnd("U0ALICE0001"), "AUTH_DEAD")
    await until(lambda: len(dead) == 1)
    assert client.health().status == "dead"


async def test_domain_apis_thread_context_and_history(started: Started) -> None:
    slack = FakeSlack()
    slack.on(
        "conversations.replies",
        lambda _f, _n: {
            "ok": True,
            "has_more": False,
            "messages": [
                {"ts": "1800000000.000100", "thread_ts": "1800000000.000100", "user": "U0ALICE0001", "text": "root"},
                {
                    "ts": "1800000000.000200",
                    "thread_ts": "1800000000.000100",
                    "user": "U0BOB000001",
                    "text": "<@U0ALICE0001> ok",
                },
            ],
        },
    )
    slack.on(
        "conversations.history",
        lambda _f, _n: {
            "ok": True,
            "has_more": False,
            "messages": [{"ts": "1800000000.000100", "user": "U0ALICE0001", "text": "root", "reply_count": 1}],
        },
    )
    _, client = await started(slack)
    ctx = await client.threads.get_context("C0GENERAL01", "1800000000.000200")
    assert ctx.root_ts == "1800000000.000100"
    assert [p.display_name for p in ctx.participants] == ["Alice", "Bob"]
    assert ctx.channel.name == "general"
    assert "Bob (@bob):\n  @Alice ok" in ctx.formatted_transcript
    page = await client.channels.get_history("C0GENERAL01", limit=10)
    assert page.messages[0].sender.display_name == "Alice"
    assert page.messages[0].thread is None
    users = await client.users.resolve(["U0ALICE0001", "U0NOBODY001"])
    assert users[0] is not None and users[0].display_name == "Alice"
    assert users[1] is None
    await expect_code(client.users.resolve("not-a-user"), "INVALID_ARGUMENT")


# ------------------------------------------------------------------------------ regression tests


async def test_frames_right_behind_hello_wait_for_backfill(started: Started) -> None:
    """``hello`` is handled synchronously, so a frame queued right behind it is buffered (§10)."""
    slack = FakeSlack()
    slack.on(
        "client.counts",
        lambda _f, _n: {
            "ok": True,
            "channels": [{"id": "C0GENERAL01", "latest": "1800000005.000100"}],
            "mpims": [],
            "ims": [],
        },
    )
    gate = asyncio.Event()

    async def history(_f: dict[str, str], _n: int) -> dict[str, Any]:
        await gate.wait()
        missed = {"type": "message", "user": "U0BOB000001", "text": "missed", "ts": "1800000002.000100"}
        return {"ok": True, "has_more": False, "messages": [missed]}

    slack.on("conversations.history", history)
    original = slack.create_web_socket

    async def factory(url: str, headers: dict[str, str]) -> Any:
        sock = await original(url, headers)
        if len(slack.sockets) == 2:
            sock.deliver(message_frame(ts="1800000004.000100", text="burst"))  # same read as hello
        return sock

    slack.create_web_socket = factory  # type: ignore[method-assign]
    _, client = await started(slack)
    seen: list[tuple[str, str]] = []
    client.on("message.created", lambda e, _ctx: seen.append((e.ts, e.delivery)))
    slack.socket.deliver(message_frame(ts="1800000001.000100"))
    await until(lambda: len(seen) == 1)
    await tick(0.02)
    client.core.ws.mark_ready(False)
    slack.socket.server_close(1006)
    await until(lambda: len(slack.sockets) == 2, timeout=5.0)
    await tick(0.05)
    gate.set()
    await until(lambda: len(seen) == 3)
    assert seen == [
        ("1800000001.000100", "realtime"),
        ("1800000002.000100", "backfill"),
        ("1800000004.000100", "realtime"),
    ]


async def test_profile_pool_cap_reply_marks_pool_full(started: Started) -> None:
    slack, client = await started()
    subscribe_requests: list[dict[str, Any]] = []

    def on_sent(frame: dict[str, Any]) -> None:
        if frame.get("type") == "subscription_update":
            subscribe_requests.append(frame)
            slack.socket.deliver(
                {
                    "type": "subscription_update",
                    "reply_to": frame["id"],
                    "ok": False,
                    "error": {"msg": "cap", "gatewayErrorCode": 12},
                }
            )

    slack.socket.on_sent(on_sent)
    await client.presence.watch(["U0ALICE0001"], track_profile_and_dnd=True)
    await until(lambda: len(subscribe_requests) >= 1)
    await tick(0.05)
    assert client.presence._profile_full is True
    # A full pool stops after the refused subscribe request (no DND request for that batch).
    assert [f["subtype"] for f in subscribe_requests] == ["user_change_subscribe_request"]


async def test_suffixed_stream_context_exit_after_finish(started: Started) -> None:
    slack, client = await started(chat=ChatSchedulerOptions(stream_updates_per_second=100, stream_min_interval=0))
    agent = client.scope(ScopePolicy(write=WritePolicy(disclosure_suffix="\n_ai_")))
    async with await agent.chat.create_stream("C0GENERAL01") as s:
        s.append("hi")
        await s.finish()
    assert slack.calls_to("chat.update")[-1].fields["markdown_text"] == "hi\n_ai_"


async def test_wait_for_reply_accepts_truthy_predicates(started: Started) -> None:
    slack, client = await started()
    got: list[MessageCreatedEvent | None] = []

    async def handler(evt: MessageCreatedEvent, ctx: Any) -> None:
        if evt.text == "ask":
            got.append(await ctx.wait_for_reply(lambda r: re.search("yes", r.text), timeout=2.0))

    client.on("message.created", handler)
    slack.socket.deliver(message_frame(text="ask", ts="1800000001.000100"))
    await tick(0.05)
    slack.socket.deliver(message_frame(text="yes", ts="1800000002.000100"))
    await until(lambda: len(got) == 1)
    assert got[0] is not None and got[0].text == "yes"


# ------------------------------------------------------------------------------- backfill reads

DM = "D0ALICE0001"
GROUP = "G0GROUP0001"


def _rich(*elements: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": list(elements)}]}]


def _with_dms(slack: FakeSlack) -> FakeSlack:
    def dms(f: dict[str, str], _n: int) -> dict[str, Any]:
        if f.get("cursor"):
            group = {"id": GROUP, "name": "mpdm-me--alice--bob-1", "is_mpim": True, "is_open": True}
            return {
                "ok": True,
                "dms": [{"id": GROUP, "latest": "1800000050.000100", "channel": group}],
                "response_metadata": {"next_cursor": ""},
            }
        alice = {"id": DM, "is_im": True, "user": "U0ALICE0001", "is_open": False}
        bob = {"id": "D0BOB000001", "is_im": True, "user": "U0BOB000001", "is_open": True}
        return {
            "ok": True,
            "dms": [{"id": DM, "latest": "1800000100.000100", "channel": alice}, {"id": "D0BOB000001", "channel": bob}],
            "response_metadata": {"next_cursor": "page2"},
        }

    slack.on("client.dms", dms)
    return slack


def _with_activity(slack: FakeSlack) -> FakeSlack:
    slack.channels[DM] = {"id": DM, "is_im": True, "user": "U0ALICE0001"}

    def at_user(key: str, ts: str, **extra: str) -> dict[str, Any]:
        message = {"ts": ts, "channel": "C0GENERAL01", **extra}
        return {"key": key, "feed_ts": ts, "is_unread": key == "k1", "item": {"type": "at_user", "message": message}}

    def feed(f: dict[str, str], _n: int) -> dict[str, Any]:
        if f.get("cursor"):
            return {"ok": True, "items": [at_user("k3", "1700000000.000100")], "response_metadata": {"next_cursor": ""}}
        latest = {"ts": "1800000008.000100", "channel": DM}
        dm = {"type": "dm", "bundle_info": {"payload": {"dm_entry": {"latest_message": latest}}}}
        return {
            "ok": True,
            "items": [
                at_user("k1", "1800000009.000100", thread_ts="1800000005.000100"),
                {"key": "k2", "feed_ts": "1800000008.000100", "is_unread": False, "item": dm},
                at_user("k4", "1800000007.000100"),
            ],
            "response_metadata": {"next_cursor": "older"},
        }

    stored: dict[str, dict[str, Any]] = {
        "1800000009.000100": {
            "user": "U0ALICE0001",
            "text": "<@U0SELF00001> ping",
            "thread_ts": "1800000005.000100",
            "blocks": _rich({"type": "user", "user_id": SELF}, {"type": "text", "text": " ping"}),
        },
        "1800000008.000100": {"user": "U0ALICE0001", "text": "hello"},
    }

    def messages_list(f: dict[str, str], _n: int) -> dict[str, Any]:
        data = {
            entry["channel"]: {
                "messages": [{"ts": ts, "type": "message", **stored[ts]} for ts in entry["timestamps"] if ts in stored]
            }
            for entry in json.loads(f["message_ids"])
        }
        return {"ok": True, "messages_data": data}

    slack.on("activity.feed", feed)
    slack.on("messages.list", messages_list)
    return slack


async def test_list_direct_conversations_with_latest_activity(started: Started) -> None:
    slack, client = await started(_with_dms(FakeSlack()))
    page = await client.channels.list_direct()
    assert page.next_cursor == "page2"
    first, second = page.items
    assert (first.channel.id, first.channel.name, first.channel.is_im) == (DM, "@Alice", True)
    assert (first.latest_ts, first.is_open) == ("1800000100.000100", False)
    assert first.peer is not None and first.peer.display_name == "Alice"
    assert (second.channel.name, second.latest_ts, second.is_open) == ("@Bob", None, True)
    fields = slack.calls_to("client.dms")[0].fields
    assert (fields["count"], fields["include_closed"], fields["include_channel"]) == ("50", "true", "true")

    recent = [d.channel.id async for d in client.channels.iterate_direct(active_since="1800000040.000100")]
    assert recent == [DM, GROUP]
    assert slack.calls_to("client.dms")[-1].fields["cursor"] == "page2"
    # Listing never marks, opens or closes a conversation.
    assert not any(re.search(r"mark|conversations\.(open|close)", c.method) for c in slack.calls)


async def test_history_messages_carry_mentions_subtype_and_deletion_state(started: Started) -> None:
    slack = FakeSlack()
    mention = {
        "ts": "1800000003.000100",
        "user": "U0ALICE0001",
        "text": "<@U0SELF00001> and <!subteam^S0ONCALL001>",
        "blocks": _rich(
            {"type": "user", "user_id": SELF},
            {"type": "text", "text": " and "},
            {"type": "usergroup", "usergroup_id": "S0ONCALL001"},
        ),
    }
    join = {"ts": "1800000002.000100", "user": "U0BOB000001", "subtype": "channel_join", "text": "joined"}
    gone = {"ts": "1800000001.000100", "user": "U0ALICE0001", "subtype": "tombstone", "text": "deleted"}
    slack.on("conversations.history", lambda _f, _n: {"ok": True, "has_more": False, "messages": [mention, join, gone]})
    _, client = await started(slack)
    first, second, third = (await client.channels.get_history("C0GENERAL01")).messages
    assert [(m.kind, m.id, m.name, m.includes_me) for m in first.mentions] == [
        ("user", SELF, "Me", None),
        ("usergroup", "S0ONCALL001", "oncall", True),
    ]
    assert first.mention_reasons == ["direct", "usergroup"]
    assert (first.subtype, first.is_deleted) == (None, False)
    assert (second.subtype, second.mention_reasons, second.is_deleted) == ("channel_join", [], False)
    assert (third.subtype, third.is_deleted) == ("tombstone", True)


async def test_activity_feed_resolves_entries_to_full_messages(started: Started) -> None:
    slack, client = await started(_with_activity(FakeSlack()))
    page = await client.activity.list(["at_user", "dm"])
    fields = slack.calls_to("activity.feed")[0].fields
    assert (fields["types"], fields["mode"], fields["limit"]) == ("at_user,dm", "chrono_reads_and_unreads", "20")
    assert len(slack.calls_to("messages.list")) == 1
    assert page.next_cursor == "older"
    k1, k2, k4 = page.items
    assert (k1.key, k1.type, k1.is_unread, k1.ts, k1.thread_ts) == (
        "k1",
        "at_user",
        True,
        "1800000009.000100",
        "1800000005.000100",
    )
    assert k1.channel is not None and (k1.channel.id, k1.channel.name) == ("C0GENERAL01", "general")
    assert k1.message is not None and k1.message.sender.display_name == "Alice"
    assert (k1.message.markdown, k1.message.mention_reasons) == ("@Me ping", ["direct"])
    assert k1.message.thread is not None and k1.message.thread.root_ts == "1800000005.000100"
    assert k2.channel is not None and (k2.type, k2.channel.is_im, k2.channel.name) == ("dm", True, "@Alice")
    assert k2.message is not None and k2.message.markdown == "hello"
    # The message behind k4 no longer exists: the entry stays, without a message.
    assert (k4.ts, k4.message) == ("1800000007.000100", None)

    keys = [i.key async for i in client.activity.iterate(["at_user", "dm"], since="1750000000.000000")]
    assert keys == ["k1", "k2", "k4"]
    marks = r"^activity\.mark|conversations\.mark|subscriptions\.thread\.mark"
    assert not any(re.search(marks, c.method) for c in slack.calls)
    await expect_code(client.activity.list([]), "INVALID_ARGUMENT")

    got = await client.channels.get_messages("C0GENERAL01", ["1800000009.000100", "1800000007.000100"])
    assert [m.ts for m in got] == ["1800000009.000100"]


async def test_scope_allow_list_covers_direct_conversations_activity_and_fetched_messages(started: Started) -> None:
    _, client = await started(_with_activity(_with_dms(FakeSlack())))
    agent = client.scope(ScopePolicy(read=ReadPolicy(channels=[DM]), write=WritePolicy(allowed_actions=[])))
    assert [d.channel.id for d in (await agent.channels.list_direct()).items] == [DM]
    assert [i.key for i in (await agent.activity.list(["at_user", "dm"])).items] == ["k2"]
    await expect_code(agent.channels.get_messages("C0GENERAL01", ["1800000009.000100"]), "SCOPE_POLICY_VIOLATION")
    unrestricted = client.scope(ScopePolicy(write=WritePolicy(allowed_actions=[])))
    assert len((await unrestricted.activity.list(["at_user"])).items) == 3


async def test_activity_resolves_many_conversations_in_calls_of_at_most_20_channels(started: Started) -> None:
    slack = FakeSlack()

    def entry(i: int) -> dict[str, Any]:
        ts = f"18000001{i:02d}.000100"
        message = {"ts": ts, "channel": f"C0CHAN{i:05d}"}
        return {"key": f"k{i}", "feed_ts": ts, "is_unread": False, "item": {"type": "at_user", "message": message}}

    def messages_list(f: dict[str, str], _n: int) -> dict[str, Any]:
        ids = json.loads(f["message_ids"])
        if len(ids) > 20:
            return {"ok": False, "error": "too_many_channels"}
        data = {
            e["channel"]: {"messages": [{"ts": ts, "user": "U0ALICE0001", "text": "hi"} for ts in e["timestamps"]]}
            for e in ids
        }
        return {"ok": True, "messages_data": data}

    slack.on("activity.feed", lambda _f, _n: {"ok": True, "items": [entry(i) for i in range(45)]})
    slack.on("messages.list", messages_list)
    _, client = await started(slack)
    page = await client.activity.list(["at_user"], limit=50)
    assert len([i for i in page.items if i.message]) == 45
    assert [len(json.loads(c.fields["message_ids"])) for c in slack.calls_to("messages.list")] == [20, 20, 5]
