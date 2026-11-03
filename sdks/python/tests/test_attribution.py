"""Agent attribution line (spec/behavior.md §9.1; port of sdks/node/test/attribution.test.ts)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import pytest

from gabriel import (
    AttributionInfo,
    AttributionPreset,
    GabrielClient,
    MessageAttribution,
    MessageCreatedEvent,
    MessageUpdatedEvent,
    ReplyOptions,
    parse_attribution,
    render_attribution,
)
from gabriel.client.chat import ChatSchedulerOptions
from gabriel.client.scope import ScopePolicy
from gabriel.errors import GabrielError
from gabriel.markdown.attribution import markdown_to_plain_text, resolve_attribution, validate_attribution
from gabriel.markdown.blocks_to_markdown import EntityResolver
from gabriel.markdown.chunker import DEFAULT_CHUNK_BUDGET, measure_markdown
from harness import SELF, FakeSlack, make_client, message_frame, tick, until

WHERE: dict[str, Any] = {"self_user_id": "U0ALICE0001", "channel": "C0GENERAL01"}
PAIMON: AttributionPreset = {"name": "Paimon", "role": "Assistant"}

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


def blocks_of(fields: dict[str, str]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = json.loads(fields["blocks"])
    return blocks


def line_of(block: dict[str, Any]) -> str:
    text: str = block["elements"][0]["text"]
    return text


# ------------------------------------------------------------------------------------ render


@pytest.mark.parametrize(
    ("review", "text"),
    [
        ("confirmed", ":robot_face: Paimon (Assistant) · confirmed by <@U0ALICE0001>"),
        ("unconfirmed", ":robot_face: Paimon (Assistant) · sent for <@U0ALICE0001> · not reviewed"),
        ("hidden", ":robot_face: Paimon (Assistant)"),
    ],
)
def test_renders_each_review(review: Any, text: str) -> None:
    r = render_attribution({**PAIMON, "review": review}, **WHERE)
    assert r.text == text
    assert r.block == {
        "type": "context",
        "block_id": f"gabriel.attribution.{review}",
        "elements": [{"type": "mrkdwn", "text": text}],
    }


def test_defaults_to_unconfirmed_and_robot_icon() -> None:
    assert (
        render_attribution({"name": "Paimon"}, **WHERE).text
        == ":robot_face: Paimon · sent for <@U0ALICE0001> · not reviewed"
    )
    assert render_attribution({"name": "Paimon", "icon": None, "review": "hidden"}, **WHERE).text == "Paimon"
    assert (
        render_attribution({"name": "Paimon", "icon": ":sparkles:", "review": "hidden"}, **WHERE).text
        == ":sparkles: Paimon"
    )
    assert (
        render_attribution({"name": "Paimon", "icon": ":wave::skin-tone-3:", "review": "hidden"}, **WHERE).text
        == ":wave::skin-tone-3: Paimon"
    )


def test_escapes_and_trims_preset_fields() -> None:
    r = render_attribution({"name": "  <!channel> & co ", "role": "a>b", "review": "hidden"}, **WHERE)
    assert r.text == ":robot_face: &lt;!channel&gt; &amp; co (a&gt;b)"


def test_custom_text_with_self_and_a_callable() -> None:
    r = render_attribution({"text": ":sparkles: drafted by *Paimon*, reviewed by {self}"}, **WHERE)
    assert r.text == ":sparkles: drafted by *Paimon*, reviewed by <@U0ALICE0001>"
    assert r.block["block_id"] == "gabriel.attribution.custom"
    seen: list[AttributionInfo] = []

    def text(info: AttributionInfo) -> str:
        seen.append(info)
        return f"via Paimon in {info.channel} ({info.review})"

    fn = render_attribution({"text": text, "review": "confirmed"}, **WHERE, thread_ts="1800000000.000100")
    assert fn.text == "via Paimon in C0GENERAL01 (confirmed)"
    assert fn.block["block_id"] == "gabriel.attribution.custom"
    assert seen == [
        AttributionInfo(
            self_user_id="U0ALICE0001", channel="C0GENERAL01", thread_ts="1800000000.000100", review="confirmed"
        )
    ]


@pytest.mark.parametrize(
    ("a", "msg"),
    [
        ({"name": ""}, "name"),
        ({"name": "   "}, "must not be empty"),
        ({"role": "Assistant"}, "`name` must be a string"),
        ({"name": "two\nlines"}, "single line"),
        ({"name": "x" * 65}, "64"),
        ({"name": "Paimon", "role": "r" * 65}, "64"),
        ({"name": "Paimon", "icon": "robot"}, "icon"),
        ({"name": "Paimon", "icon": ":robot face:"}, "icon"),
        ({"name": "Paimon", "review": "maybe"}, "review"),
        ({"text": ""}, "`text` must not be empty"),
        ({"text": "   "}, "empty"),
        ({"text": 42}, "string or a function"),
        ({"text": "hey <!here>"}, "@channel"),
        ({"text": "hey <!CHANNEL>"}, "@channel"),
        ({"text": "hey <!subteam^S0ONCALL001>"}, "user group"),
        ({"text": "\U0001f389" * 160}, "3000"),
        ("Paimon", "must be an object"),
    ],
)
def test_rejects_invalid_attribution(a: Any, msg: str) -> None:
    with pytest.raises(GabrielError, match=msg) as info:
        render_attribution(a, **WHERE)
    assert info.value.code == "INVALID_ARGUMENT"


def test_rejects_a_broadcast_mention_returned_by_a_text_callable() -> None:
    with pytest.raises(GabrielError, match="@channel"):
        render_attribution({"text": lambda _info: "<!everyone>"}, **WHERE)
    with pytest.raises(GabrielError, match="must return a string"):
        render_attribution({"text": lambda _info: None}, **WHERE)  # type: ignore[arg-type,return-value]


def test_confirmed_is_per_write_only() -> None:
    validate_attribution({**PAIMON, "review": "confirmed"}, "write")
    with pytest.raises(GabrielError, match="per write"):
        validate_attribution({**PAIMON, "review": "confirmed"}, "client")
    with pytest.raises(GabrielError, match="per write"):
        validate_attribution({"text": "via Paimon", "review": "confirmed"}, "client")
    validate_attribution({**PAIMON, "review": "hidden"}, "client")


# ----------------------------------------------------------------------------------- resolve


def test_resolve_merges_a_partial_preset_and_false_turns_it_off() -> None:
    assert resolve_attribution(PAIMON, {"review": "confirmed"}) == {**PAIMON, "review": "confirmed"}
    assert resolve_attribution(PAIMON, None) == PAIMON
    assert resolve_attribution(PAIMON, False) is None
    assert resolve_attribution(None, None) is None
    assert resolve_attribution({**PAIMON, "icon": ":sparkles:"}, {"name": "Other"}) == {
        "name": "Other",
        "role": "Assistant",
        "icon": ":sparkles:",
    }
    assert resolve_attribution(PAIMON, {"icon": None}) == {**PAIMON, "icon": None}


def test_resolve_keeps_a_custom_default_when_only_review_changes() -> None:
    custom: Any = {"text": "via Paimon"}
    assert resolve_attribution(custom, {"review": "confirmed"}) == {"text": "via Paimon", "review": "confirmed"}
    assert resolve_attribution(custom, {}) == custom
    assert resolve_attribution(custom, {"name": "Other"}) == {"name": "Other"}
    assert resolve_attribution(PAIMON, {"text": "x"}) == {"text": "x"}


def test_resolve_requires_a_name_without_a_default() -> None:
    with pytest.raises(GabrielError, match="name") as info:
        resolve_attribution(None, {"review": "confirmed"})
    assert info.value.code == "INVALID_ARGUMENT"
    with pytest.raises(GabrielError, match="must be an object"):
        resolve_attribution(PAIMON, True)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- parse / plain text


def test_parses_the_leading_block_only() -> None:
    block = render_attribution({**PAIMON, "review": "confirmed"}, **WHERE).block
    section = {"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}
    assert parse_attribution([block, section], EntityResolver(user_name=lambda _id: "Alice")) == MessageAttribution(
        text=":robot_face: Paimon (Assistant) · confirmed by @Alice", review="confirmed"
    )
    assert parse_attribution([section, block]) is None
    bare = parse_attribution([{**block, "block_id": "gabriel.attribution"}])
    assert bare is not None and bare.review is None and bare.text
    custom = parse_attribution([{**block, "block_id": "gabriel.attribution.custom"}])
    assert custom is not None and custom.review is None
    assert parse_attribution([{**block, "block_id": "gabriel.attributions"}]) is None
    assert parse_attribution([{**block, "type": "section"}]) is None
    assert parse_attribution(None) is None
    assert parse_attribution([]) is None


def test_markdown_to_plain_text() -> None:
    md = "# Title\n\n**bold** and [link](https://example.com)\n\n```ts\nconst x = 1;\n```"
    assert markdown_to_plain_text(md) == "Title\n\nbold and link\n\nconst x = 1;"
    assert markdown_to_plain_text("> quoted\n> __twice__ ~~gone~~\n\n\n\nend") == "quoted\ntwice gone\n\nend"
    long = markdown_to_plain_text("a" * 5000)
    assert len(long) == 3000 and long.endswith("…")


# ------------------------------------------------------------------------------------ client


def test_rejects_an_invalid_default_at_construction() -> None:
    slack = FakeSlack()
    with pytest.raises(GabrielError, match="per write"):
        make_client(slack, attribution={**PAIMON, "review": "confirmed"})
    with pytest.raises(GabrielError, match="name"):
        make_client(slack, attribution={"name": ""})
    with pytest.raises(GabrielError, match="icon"):
        make_client(slack, attribution={"name": "Paimon", "icon": "robot"})


async def test_sends_attribution_and_markdown_blocks_with_text_fallback(started: Started) -> None:
    slack, client = await started(attribution=PAIMON)
    await client.chat.send("C0GENERAL01", "**done**")
    f = slack.calls_to("chat.postMessage")[0].fields
    assert "markdown_text" not in f
    assert f["text"] == "done"
    assert blocks_of(f) == [
        {
            "type": "context",
            "block_id": "gabriel.attribution.unconfirmed",
            "elements": [
                {"type": "mrkdwn", "text": f":robot_face: Paimon (Assistant) · sent for <@{SELF}> · not reviewed"}
            ],
        },
        {"type": "markdown", "text": "**done**"},
    ]


async def test_text_fallback_is_an_ellipsis_for_markup_only_markdown(started: Started) -> None:
    slack, client = await started(attribution=PAIMON)
    await client.chat.send("C0GENERAL01", "```\n```")
    assert slack.calls_to("chat.postMessage")[0].fields["text"] == "…"


async def test_applies_per_write_overrides_and_false(started: Started) -> None:
    slack, client = await started(attribution=PAIMON)
    await client.chat.send("C0GENERAL01", "a", attribution={"review": "confirmed"})
    await client.chat.send("C0GENERAL01", "b", attribution=False)
    await client.chat.send("C0GENERAL01", "c", attribution={"text": "via {self}"})
    a, b, c = (call.fields for call in slack.calls_to("chat.postMessage"))
    assert blocks_of(a)[0]["block_id"] == "gabriel.attribution.confirmed"
    assert b["markdown_text"] == "b"
    assert "blocks" not in b and "text" not in b
    assert line_of(blocks_of(c)[0]) == f"via <@{SELF}>"
    assert blocks_of(c)[0]["block_id"] == "gabriel.attribution.custom"


async def test_invalid_override_is_rejected_before_sending(started: Started) -> None:
    slack, client = await started(attribution=PAIMON)
    with pytest.raises(GabrielError) as info:
        await client.chat.send("C0GENERAL01", "x", attribution={"text": "<!here> look"})
    assert info.value.code == "INVALID_ARGUMENT"
    assert slack.calls_to("chat.postMessage") == []


async def test_leaves_writes_unchanged_without_a_default(started: Started) -> None:
    slack, client = await started()
    await client.chat.send("C0GENERAL01", "plain")
    assert slack.calls_to("chat.postMessage")[0].fields["markdown_text"] == "plain"
    assert client.chat.preview_attribution() is None
    one = await client.chat.send("C0GENERAL01", "x", attribution={**PAIMON, "review": "hidden"})
    assert one.ts
    assert blocks_of(slack.calls_to("chat.postMessage")[1].fields)[0]["block_id"] == "gabriel.attribution.hidden"
    with pytest.raises(GabrielError, match="name"):
        await client.chat.send("C0GENERAL01", "y", attribution={"review": "confirmed"})


async def test_prepends_the_attribution_block_to_caller_blocks(started: Started) -> None:
    slack, client = await started(attribution=PAIMON)
    body = {"type": "section", "text": {"type": "mrkdwn", "text": "hi"}}
    await client.chat.send("C0GENERAL01", blocks=[body])
    f = slack.calls_to("chat.postMessage")[0].fields
    assert [b["type"] for b in blocks_of(f)] == ["context", "section"]
    assert "text" not in f


async def test_every_auto_chunked_message_carries_the_block_within_the_reduced_budget(started: Started) -> None:
    slack, client = await started(attribution=PAIMON)
    paragraph = ("word " * 400).strip()
    res = await client.chat.send("C0GENERAL01", "\n\n".join([paragraph] * 12))
    assert len(res.chunk_ts) > 1
    calls = slack.calls_to("chat.postMessage")
    assert len(calls) == len(res.chunk_ts)
    for call in calls:
        attr, body = blocks_of(call.fields)
        assert attr["block_id"] == "gabriel.attribution.unconfirmed"
        total = measure_markdown(line_of(attr)).effective + measure_markdown(body["text"]).effective
        assert total <= DEFAULT_CHUNK_BUDGET


async def test_server_side_resplit_keeps_the_block(started: Started) -> None:
    slack = FakeSlack()
    posted: list[dict[str, str]] = []

    def post(f: dict[str, str], n: int) -> dict[str, Any]:
        if n == 1:
            return {"ok": False, "error": "msg_too_long"}
        posted.append(f)
        slack.posted_ts += 1
        return {"ok": True, "channel": f["channel"], "ts": f"{slack.posted_ts}.000100"}

    slack.on("chat.postMessage", post)
    _, client = await started(slack, attribution=PAIMON)
    paragraph = ("word " * 400).strip()
    res = await client.chat.send("C0GENERAL01", "\n\n".join([paragraph] * 5))
    assert len(res.chunk_ts) > 1
    for f in posted:
        assert blocks_of(f)[0]["block_id"] == "gabriel.attribution.unconfirmed"


async def test_keeps_the_block_on_chat_update(started: Started) -> None:
    slack, client = await started(attribution=PAIMON)
    sent = await client.chat.send("C0GENERAL01", "v1")
    await client.chat.update("C0GENERAL01", sent.ts, "v2", attribution={"review": "confirmed"})
    f = slack.calls_to("chat.update")[0].fields
    assert "markdown_text" not in f
    assert [b.get("block_id", b["type"]) for b in blocks_of(f)] == ["gabriel.attribution.confirmed", "markdown"]
    assert f["text"] == "v2"
    await client.chat.update("C0GENERAL01", sent.ts, blocks=[{"type": "divider"}])
    assert [b["type"] for b in blocks_of(slack.calls_to("chat.update")[1].fields)] == ["context", "divider"]


async def test_stream_placeholder_and_every_refresh_carry_the_block(started: Started) -> None:
    slack = FakeSlack()
    slack.on("chat.update", lambda f, _n: {"ok": True, "ts": f["ts"], "channel": f["channel"]})
    _, client = await started(
        slack, attribution=PAIMON, chat=ChatSchedulerOptions(stream_updates_per_second=100, stream_min_interval=0)
    )
    stream = await client.chat.create_stream("C0GENERAL01")
    stream.append("hello ")
    await tick(0.005)
    stream.append("world")
    await stream.finish()
    writes = [*slack.calls_to("chat.postMessage"), *slack.calls_to("chat.update")]
    assert len(writes) >= 2
    for w in writes:
        assert blocks_of(w.fields)[0]["block_id"] == "gabriel.attribution.unconfirmed"
    assert blocks_of(slack.calls_to("chat.update")[-1].fields)[1] == {"type": "markdown", "text": "hello world"}


async def test_stream_continuation_carries_the_block(started: Started) -> None:
    slack = FakeSlack()
    slack.on("chat.update", lambda f, _n: {"ok": True, "ts": f["ts"], "channel": f["channel"]})
    _, client = await started(
        slack, attribution=PAIMON, chat=ChatSchedulerOptions(stream_updates_per_second=1000, stream_min_interval=0)
    )
    stream = await client.chat.create_stream("C0GENERAL01", attribution={"review": "hidden"})
    paragraph = ("word " * 400).strip()
    for _ in range(8):
        stream.append(paragraph + "\n\n")
        await tick(0.005)
    await stream.finish()
    posts = slack.calls_to("chat.postMessage")
    assert len(posts) >= 2
    for w in [*posts, *slack.calls_to("chat.update")]:
        attr, body = blocks_of(w.fields)
        assert attr["block_id"] == "gabriel.attribution.hidden"
        assert measure_markdown(line_of(attr)).effective + measure_markdown(body["text"]).effective <= (
            DEFAULT_CHUNK_BUDGET
        )


async def test_stream_override_false_sends_plain_markdown(started: Started) -> None:
    slack, client = await started(
        attribution=PAIMON, chat=ChatSchedulerOptions(stream_updates_per_second=100, stream_min_interval=0)
    )
    async with client.chat.stream("C0GENERAL01", attribution=False) as s:
        s.append("plain")
    assert slack.calls_to("chat.postMessage")[0].fields["markdown_text"] == "…"
    assert slack.calls_to("chat.update")[-1].fields["markdown_text"] == "plain"


async def test_passes_ctx_reply_and_reply_stream_overrides_through(started: Started) -> None:
    slack, client = await started(
        attribution=PAIMON, chat=ChatSchedulerOptions(stream_updates_per_second=100, stream_min_interval=0)
    )

    async def handler(evt: MessageCreatedEvent, ctx: Any) -> None:
        if evt.markdown == "hi":
            await ctx.reply("thanks", attribution={"review": "confirmed"})
        elif evt.markdown == "opts":
            await ctx.reply(ReplyOptions(markdown="ok", attribution=False))
        else:
            s = await ctx.reply_stream(attribution={"review": "hidden"})
            s.append("streamed")
            await s.finish()

    client.on("message.created", handler)
    slack.socket.deliver(message_frame(text="hi", ts="1800000001.000100"))
    await until(lambda: len(slack.calls_to("chat.postMessage")) == 1)
    assert blocks_of(slack.calls_to("chat.postMessage")[0].fields)[0]["block_id"] == "gabriel.attribution.confirmed"
    slack.socket.deliver(message_frame(text="opts", ts="1800000002.000100", event_ts="1800000002.000100"))
    await until(lambda: len(slack.calls_to("chat.postMessage")) == 2)
    assert slack.calls_to("chat.postMessage")[1].fields["markdown_text"] == "ok"
    slack.socket.deliver(message_frame(text="stream", ts="1800000003.000100", event_ts="1800000003.000100"))
    await until(lambda: len(slack.calls_to("chat.postMessage")) == 3)
    assert blocks_of(slack.calls_to("chat.postMessage")[2].fields)[0]["block_id"] == "gabriel.attribution.hidden"


async def test_previews_the_exact_line_without_sending(started: Started) -> None:
    slack, client = await started(attribution=PAIMON)
    preview = client.chat.preview_attribution({"review": "confirmed"})
    assert preview is not None
    assert preview.text == f":robot_face: Paimon (Assistant) · confirmed by <@{SELF}>"
    assert preview.block["block_id"] == "gabriel.attribution.confirmed"
    assert client.chat.preview_attribution(False) is None
    default = client.chat.preview_attribution()
    assert default is not None and default.block["block_id"] == "gabriel.attribution.unconfirmed"
    seen: list[AttributionInfo] = []

    def text(info: AttributionInfo) -> str:
        seen.append(info)
        return "via Paimon"

    client.chat.preview_attribution({"text": text}, channel="C0GENERAL01", thread_ts="1800000000.000100")
    assert seen[0].channel == "C0GENERAL01" and seen[0].thread_ts == "1800000000.000100"
    assert seen[0].self_user_id == SELF
    assert slack.calls_to("chat.postMessage") == []


async def test_scoped_view_denies_overrides_but_applies_the_default(started: Started) -> None:
    slack, client = await started(
        attribution=PAIMON, chat=ChatSchedulerOptions(stream_updates_per_second=100, stream_min_interval=0)
    )
    agent = client.scope(ScopePolicy())
    for override in ({"review": "confirmed"}, False):
        with pytest.raises(GabrielError) as info:
            await agent.chat.send("C0GENERAL01", "x", attribution=override)
        assert info.value.code == "SCOPE_POLICY_VIOLATION"
    with pytest.raises(GabrielError) as info:
        await agent.chat.update("C0GENERAL01", "1800000000.000100", "x", attribution=False)
    assert info.value.code == "SCOPE_POLICY_VIOLATION"
    with pytest.raises(GabrielError) as info:
        await agent.chat.create_stream("C0GENERAL01", attribution={"review": "confirmed"})
    assert info.value.code == "SCOPE_POLICY_VIOLATION"
    assert slack.calls_to("chat.postMessage") == []
    await agent.chat.send("C0GENERAL01", "y")
    assert blocks_of(slack.calls_to("chat.postMessage")[0].fields)[0]["block_id"] == "gabriel.attribution.unconfirmed"


# ---------------------------------------------------------------------------------- received

_LINE = {
    "type": "context",
    "block_id": "gabriel.attribution.confirmed",
    "elements": [{"type": "mrkdwn", "text": ":robot_face: Paimon · confirmed by <@U0ALICE0001>"}],
}


def _body(text: str) -> dict[str, Any]:
    section = {"type": "rich_text_section", "elements": [{"type": "text", "text": text}]}
    return {"type": "rich_text", "elements": [section]}


async def test_received_message_strips_the_line_and_exposes_it(started: Started) -> None:
    slack, client = await started()
    got: list[MessageCreatedEvent] = []
    client.on(MessageCreatedEvent, lambda e, _ctx: got.append(e))
    slack.socket.deliver(message_frame(text="see you", blocks=[_LINE, _body("see you")]))
    await until(lambda: len(got) == 1)
    evt = got[0]
    assert evt.markdown == "see you"
    assert evt.attribution == MessageAttribution(text=":robot_face: Paimon · confirmed by @Alice", review="confirmed")
    assert evt.mentions == []
    assert evt.to_dict()["attribution"] == {"text": ":robot_face: Paimon · confirmed by @Alice", "review": "confirmed"}
    assert (
        "attribution: :robot_face: Paimon · confirmed by @Alice (self-declared by the sender, unverified)"
        in evt.to_llm_context()
    )


async def test_received_message_without_attribution(started: Started) -> None:
    slack, client = await started()
    got: list[MessageCreatedEvent] = []
    client.on(MessageCreatedEvent, lambda e, _ctx: got.append(e))
    slack.socket.deliver(message_frame(text="plain"))
    await until(lambda: len(got) == 1)
    assert got[0].attribution is None
    assert "attribution" not in got[0].to_dict()
    assert "attribution:" not in got[0].to_llm_context()


async def test_message_updated_exposes_the_current_line(started: Started) -> None:
    slack, client = await started()
    got: list[MessageUpdatedEvent] = []
    client.on(MessageUpdatedEvent, lambda e, _ctx: got.append(e))
    slack.socket.deliver(
        {
            "type": "message",
            "subtype": "message_changed",
            "channel": "C0GENERAL01",
            "ts": "1800000002.000200",
            "event_ts": "1800000002.000200",
            "message": {
                "type": "message",
                "user": "U0ALICE0001",
                "ts": "1800000001.000100",
                "text": "v2",
                "blocks": [_LINE, _body("v2")],
                "edited": {"ts": "1800000002.000200"},
            },
            "previous_message": {
                "type": "message",
                "user": "U0ALICE0001",
                "ts": "1800000001.000100",
                "text": "v1",
                "blocks": [_LINE, _body("v1")],
            },
        }
    )
    await until(lambda: len(got) == 1)
    assert got[0].current_markdown == "v2"
    assert got[0].previous_markdown == "v1"
    assert got[0].attribution is not None and got[0].attribution.review == "confirmed"


async def test_thread_context_and_history_mark_attributed_messages(started: Started) -> None:
    slack = FakeSlack()
    messages = [
        {
            "type": "message",
            "user": "U0ALICE0001",
            "ts": "1800000001.000100",
            "thread_ts": "1800000001.000100",
            "text": "question",
        },
        {
            "type": "message",
            "user": "U0BOB000001",
            "ts": "1800000001.000200",
            "thread_ts": "1800000001.000100",
            "text": "answer",
            "blocks": [_LINE, _body("answer")],
        },
    ]
    slack.on("conversations.replies", lambda _f, _n: {"ok": True, "has_more": False, "messages": messages})
    slack.on("conversations.history", lambda _f, _n: {"ok": True, "has_more": False, "messages": messages[1:]})
    _, client = await started(slack)
    ctx = await client.threads.get_context("C0GENERAL01", "1800000001.000100")
    assert ctx.messages[0].attribution is None
    assert ctx.messages[1].markdown == "answer"
    assert ctx.messages[1].attribution == MessageAttribution(
        text=":robot_face: Paimon · confirmed by @Alice", review="confirmed"
    )
    assert "Bob (@bob) [attribution: :robot_face: Paimon · confirmed by @Alice]:" in ctx.formatted_transcript
    page = await client.channels.get_history("C0GENERAL01", limit=10)
    assert page.messages[0].markdown == "answer"
    assert page.messages[0].attribution is not None and page.messages[0].attribution.review == "confirmed"


async def test_round_trip_through_the_harness_echo(started: Started) -> None:
    slack, client = await started(attribution=PAIMON)
    await client.chat.send("C0GENERAL01", "**done**", attribution={"review": "confirmed"})
    f = slack.calls_to("chat.postMessage")[0].fields
    echoed = {"type": "message", "user": SELF, "text": f["text"], "blocks": blocks_of(f)}
    parsed = parse_attribution(echoed["blocks"], EntityResolver(user_name=lambda _id: "Me"))
    assert parsed == MessageAttribution(text=":robot_face: Paimon (Assistant) · confirmed by @Me", review="confirmed")
