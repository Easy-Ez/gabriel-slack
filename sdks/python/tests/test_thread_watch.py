"""threads.watch(): local-only thread watch (spec/behavior.md §10.1)."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from gabriel.client.apis import ThreadWatchResult, WatchedThread
from gabriel.client.client import GabrielClient, RecoveryOptions
from gabriel.core.types import RawWsFrame
from gabriel.errors import GabrielError
from gabriel.events.types import ConnectionGapEvent, MessageCreatedEvent, MessageThreadMetaUpdatedEvent
from gabriel.state.store import MemoryStateStore
from harness import FakeSlack, make_client, message_frame, tick, until

CH = "C0GENERAL01"
ROOT = "1800000100.000100"
OTHER_ROOT = "1800000200.000100"

#: Every Web API method name that writes, marks or subscribes.
WRITE_METHOD = re.compile(
    r"^(chat\.|subscriptions\.|reactions\.|files\.|conversations\.(mark|join|leave|open|close)|.*\.mark$)"
)


class SharedStore(MemoryStateStore):
    persistent = True


Factory = Callable[..., GabrielClient]


@pytest.fixture
async def new_client() -> AsyncIterator[Factory]:
    clients: list[GabrielClient] = []
    fakes: list[FakeSlack] = []

    def factory(slack: FakeSlack, **extra: Any) -> GabrielClient:
        fakes.append(slack)
        client: GabrielClient = make_client(slack, **extra)
        clients.append(client)
        return client

    yield factory
    for c in clients:
        try:
            await c.stop(drain=0.1)
        except Exception:
            pass
    for f in fakes:
        await f.aclose()


async def started(new_client: Factory, slack: FakeSlack | None = None, **extra: Any) -> tuple[FakeSlack, GabrielClient]:
    fake = slack or FakeSlack()
    client = new_client(fake, **extra)
    await client.start()
    return fake, client


def serve_thread(slack: FakeSlack, replies: list[str], root: str = ROOT) -> None:
    """A thread whose replies honour ``oldest`` / ``inclusive=false`` like Slack does."""

    def handler(f: dict[str, str], _n: int) -> dict[str, Any]:
        if f.get("ts") != root:
            return {"ok": True, "has_more": False, "messages": []}
        oldest = f.get("oldest")
        newer = [ts for ts in replies if not oldest or float(ts) > float(oldest)]
        return {
            "ok": True,
            "has_more": False,
            "messages": [
                {"type": "message", "user": "U0BOB000001", "text": "root", "ts": root, "thread_ts": root},
                *(
                    {"type": "message", "user": "U0ALICE0001", "text": f"reply {ts}", "ts": ts, "thread_ts": root}
                    for ts in newer
                ),
            ],
        }

    slack.on("conversations.replies", handler)


def reply_frame(ts: str, root: str = ROOT) -> RawWsFrame:
    return message_frame(ts=ts, event_ts=ts, thread_ts=root, text=f"reply {ts}")


def replied_frame(latest: str, root: str = ROOT) -> RawWsFrame:
    ts = f"{latest.split('.')[0]}.000900"
    return {
        "type": "message",
        "subtype": "message_replied",
        "channel": CH,
        "hidden": True,
        "ts": ts,
        "event_ts": ts,
        "message": {
            "type": "message",
            "user": "U0BOB000001",
            "text": "root",
            "ts": root,
            "thread_ts": root,
            "reply_count": 3,
            "latest_reply": latest,
        },
    }


async def cold_reconnect(client: GabrielClient, slack: FakeSlack) -> None:
    sockets = len(slack.sockets)
    client.core.ws.mark_ready(False)
    slack.socket.server_close(1006)  # no fast-reconnect ticket → cold reconnect
    await until(lambda: len(slack.sockets) == sockets + 1, timeout=5.0, label="reconnect")


async def expect_code(aw: Awaitable[Any], code: str) -> None:
    with pytest.raises(GabrielError) as info:
        await aw
    assert info.value.code == code


async def test_watch_never_sends_a_write_request(new_client: Factory) -> None:
    slack = FakeSlack()
    serve_thread(slack, ["1800000101.000100"])
    _, client = await started(new_client, slack)
    before = len(slack.calls)
    sent_before = len(slack.socket.sent)
    res = await client.threads.watch(CH, ROOT)
    client.threads.unwatch(CH, ROOT)
    methods = [c.method for c in slack.calls[before:]]
    assert res == ThreadWatchResult(recovered=1, complete=True)
    assert methods == ["conversations.replies"]
    assert [m for m in methods if WRITE_METHOD.match(m)] == []
    assert [s.get("type") for s in slack.socket.sent[sent_before:] if s.get("type") != "ping"] == []


async def test_backfills_after_after_deduplicated_and_waits_for_handlers(new_client: Factory) -> None:
    slack = FakeSlack()
    serve_thread(slack, ["1800000101.000100", "1800000102.000100", "1800000103.000100"])
    _, client = await started(new_client, slack)
    seen: list[tuple[str, str, str]] = []
    handled = 0

    async def on_created(e: MessageCreatedEvent, _ctx: Any) -> None:
        nonlocal handled
        seen.append((e.ts, e.delivery, e.origin))
        await tick(0.03)
        handled += 1

    client.on("message.created", on_created)
    slack.socket.deliver(reply_frame("1800000103.000100"))
    await until(lambda: handled == 1)

    res = await client.threads.watch(CH, ROOT, after="1800000101.000100")
    assert res == ThreadWatchResult(recovered=1, complete=True)
    assert handled == 2  # returned only after the backfilled event's handler finished
    fields = slack.calls_to("conversations.replies")[0].fields
    assert (fields["channel"], fields["ts"], fields["oldest"], fields["inclusive"]) == (
        CH,
        ROOT,
        "1800000101.000100",
        "false",
    )
    assert seen == [
        ("1800000103.000100", "realtime", "other_user"),
        ("1800000102.000100", "backfill", "other_user"),
    ]
    assert client.threads.watched() == [WatchedThread(channel=CH, thread_ts=ROOT, watermark="1800000103.000100")]

    # Repeating the call is idempotent: nothing new is delivered.
    assert await client.threads.watch(CH, ROOT) == ThreadWatchResult(recovered=0, complete=True)
    assert len(seen) == 2


async def test_only_ever_raises_the_watermark(new_client: Factory) -> None:
    _, client = await started(new_client)
    await client.threads.watch(CH, ROOT, after="1800000150.000100")
    await client.threads.watch(CH, ROOT, after="1800000120.000100")
    assert client.threads.watched() == [WatchedThread(channel=CH, thread_ts=ROOT, watermark="1800000150.000100")]


async def test_rejects_invalid_arguments_and_survives_api_failures(new_client: Factory) -> None:
    slack = FakeSlack()
    slack.on("conversations.replies", lambda _f, _n: {"ok": False, "error": "channel_not_found"})
    _, client = await started(new_client, slack)
    await expect_code(client.threads.watch("not-a-channel", ROOT), "INVALID_ARGUMENT")
    await expect_code(client.threads.watch(CH, "yesterday"), "INVALID_ARGUMENT")
    await expect_code(client.threads.watch(CH, ROOT, after="nope"), "INVALID_ARGUMENT")
    assert client.threads.watched() == []

    assert await client.threads.watch(CH, ROOT) == ThreadWatchResult(recovered=0, complete=False)
    assert client.threads.watched() == [WatchedThread(channel=CH, thread_ts=ROOT, watermark=ROOT)]


async def test_needs_a_started_client(new_client: Factory) -> None:
    client = new_client(FakeSlack())
    await expect_code(client.threads.watch(CH, ROOT), "INVALID_STATE")


async def test_keeps_recovering_after_a_restart_with_the_same_store(new_client: Factory) -> None:
    store = SharedStore()
    slack = FakeSlack()
    serve_thread(slack, [])
    _, first = await started(new_client, slack, state_store=store)
    await first.threads.watch(CH, ROOT, after="1800000101.000100")
    await first.stop(drain=0.1)

    slack2 = FakeSlack()
    serve_thread(slack2, ["1800000101.000100", "1800000104.000100"])
    client = new_client(slack2, state_store=store)
    seen: list[tuple[str, str]] = []
    gaps: list[ConnectionGapEvent] = []
    client.on("message.created", lambda e, _ctx: seen.append((e.ts, e.delivery)))
    client.on(ConnectionGapEvent, lambda e, _ctx: gaps.append(e))
    await client.start()
    await until(lambda: len(seen) == 1 and len(gaps) == 1)
    assert seen == [("1800000104.000100", "backfill")]
    fields = slack2.calls_to("conversations.replies")[0].fields
    assert (fields["ts"], fields["oldest"]) == (ROOT, "1800000101.000100")
    gap = gaps[0].to_dict()
    assert gap["cause"] == "cold_resync"
    assert gap["recovered"] is True
    assert gap["threadsRecovered"] == [{"channel": CH, "threadTs": ROOT}]
    await until(lambda: [t.watermark for t in client.threads.watched()] == ["1800000104.000100"])
    assert [c for c in slack2.calls if WRITE_METHOD.match(c.method)] == []


async def test_message_replied_triggers_a_fetch_when_the_reply_does_not_arrive(new_client: Factory) -> None:
    slack = FakeSlack()
    replies: list[str] = []
    serve_thread(slack, replies)
    _, client = await started(new_client, slack, recovery=RecoveryOptions(thread_reply_grace=0.05))
    created: list[MessageCreatedEvent] = []
    meta: list[MessageThreadMetaUpdatedEvent] = []
    client.on("message.created", lambda e, _ctx: created.append(e))
    client.on(MessageThreadMetaUpdatedEvent, lambda e, _ctx: meta.append(e))
    await client.threads.watch(CH, ROOT)

    def fetches() -> int:
        return len(slack.calls_to("conversations.replies"))

    assert fetches() == 1

    replies.append("1800000105.000100")
    slack.socket.deliver(replied_frame("1800000105.000100"))
    await until(lambda: len(created) == 1)
    assert len(meta) == 1  # the original event is still emitted
    assert (created[0].ts, created[0].delivery, created[0].is_thread_reply) == ("1800000105.000100", "backfill", True)
    assert fetches() == 2
    assert slack.calls_to("conversations.replies")[1].fields["oldest"] == ROOT

    # The reply arrives in realtime within the grace period: no fetch.
    replies.append("1800000106.000100")
    slack.socket.deliver(replied_frame("1800000106.000100"))
    slack.socket.deliver(reply_frame("1800000106.000100"))
    await until(lambda: len(created) == 2)
    await tick(0.12)
    assert fetches() == 2
    assert (created[1].ts, created[1].delivery) == ("1800000106.000100", "realtime")

    # Threads that are not explicitly watched are left alone.
    slack.socket.deliver(replied_frame("1800000201.000100", OTHER_ROOT))
    await tick(0.12)
    assert fetches() == 2


async def test_not_evicted_by_the_activity_lru_and_bounded_on_its_own(new_client: Factory) -> None:
    slack = FakeSlack()
    slack.on("subscriptions.thread.add", lambda _f, _n: {"ok": True})
    _, client = await started(
        new_client, slack, recovery=RecoveryOptions(watched_threads_limit=1, max_explicit_thread_watches=2)
    )
    await client.threads.watch(CH, ROOT)
    await client.threads.subscribe(CH, "1800000300.000100")
    await client.threads.subscribe(CH, "1800000400.000100")  # evicts 300 from the LRU
    assert [t.thread_ts for t in client.threads.watched()] == [ROOT]

    await client.threads.watch(CH, OTHER_ROOT)
    await expect_code(client.threads.watch(CH, "1800000500.000100"), "SUBSCRIPTION_CAP_EXCEEDED")
    assert (await client.threads.watch(CH, ROOT)).complete is True  # already watched

    before = len(slack.calls_to("conversations.replies"))
    await cold_reconnect(client, slack)
    await until(lambda: len(slack.calls_to("conversations.replies")) >= before + 3)
    await tick(0.02)
    fetched = sorted(c.fields["ts"] for c in slack.calls_to("conversations.replies")[before:])
    assert fetched == sorted([ROOT, OTHER_ROOT, "1800000400.000100"])


async def test_stops_recovering_after_unwatch(new_client: Factory) -> None:
    slack = FakeSlack()
    serve_thread(slack, [])
    _, client = await started(new_client, slack, recovery=RecoveryOptions(thread_reply_grace=0.02))
    await client.threads.watch(CH, ROOT)
    calls_before = len(slack.calls)
    client.threads.unwatch(CH, ROOT)
    client.threads.unwatch(CH, ROOT)  # idempotent
    assert len(slack.calls) == calls_before  # unwatch calls nothing on Slack
    assert client.threads.watched() == []

    replies_before = len(slack.calls_to("conversations.replies"))
    slack.socket.deliver(replied_frame("1800000107.000100"))
    await tick(0.08)
    await cold_reconnect(client, slack)
    await tick(0.1)
    assert len(slack.calls_to("conversations.replies")) == replies_before


async def test_does_not_deadlock_inside_a_handler_of_the_same_thread(new_client: Factory) -> None:
    slack = FakeSlack()
    serve_thread(slack, ["1800000101.000100", "1800000102.000100"])
    _, client = await started(new_client, slack)
    seen: list[str] = []
    results: list[ThreadWatchResult] = []

    async def on_created(e: MessageCreatedEvent, _ctx: Any) -> None:
        seen.append(e.ts)
        if e.delivery == "realtime":
            results.append(await client.threads.watch(CH, ROOT))

    client.on("message.created", on_created)
    slack.socket.deliver(reply_frame("1800000102.000100"))
    await until(lambda: len(results) == 1 and len(seen) == 2)
    assert results == [ThreadWatchResult(recovered=1, complete=True)]
    assert seen == ["1800000102.000100", "1800000101.000100"]
