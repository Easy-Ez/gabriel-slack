# gabriel-slack (Python)

Python / asyncio implementation of [Gabriel](https://github.com/Easy-Ez/gabriel-slack/blob/main/README.md): a typed,
AI-friendly real-time SDK for the Slack Web client protocol. It implements the same contract as
the TypeScript reference SDK.

> Unofficial and not affiliated with Slack. It acts as the signed-in person: use it only
> where you are authorized to automate, and treat the session cookie like a password.

* Python **≥ 3.11**, asyncio, fully typed (`py.typed`, `mypy --strict` clean).
* Runtime dependencies: [`httpx`](https://www.python-httpx.org/) and [`websockets`](https://websockets.readthedocs.io/).
* Behavior contract: [`spec/behavior.md`](https://github.com/Easy-Ez/gabriel-slack/blob/main/spec/behavior.md); every
  [conformance fixture](https://github.com/Easy-Ez/gabriel-slack/tree/main/spec/fixtures/normalize) passes and every
  event validates against [`events.schema.json`](https://github.com/Easy-Ez/gabriel-slack/blob/main/spec/schema/events.schema.json).

Install from PyPI (the distribution is `gabriel-slack`, the import package `gabriel`):

```bash
pip install gabriel-slack      # or: uv add gabriel-slack
```

## Contents

1. [Credentials](#credentials)
2. [Quick start](#quick-start)
3. [Events](#events)
4. [Handler context](#handler-context)
5. [Actions](#actions)
6. [AI safety: `scope()` and `to_llm_context()`](#ai-safety)
7. [Reliability](#reliability)
8. [Errors](#errors)
9. [Low-level core](#low-level-core)
10. [Configuration reference](#configuration-reference)
11. [CLI](#cli)
12. [Python conventions](#python-conventions)
13. [Development](#development)

## Credentials

The only required credential is the `d` cookie (`xoxd-…`) of a signed-in Slack web session.
The SDK exchanges it for the workspace token (`xoxc-…`), team ID and user ID, refreshes the
token on `invalid_auth` (concurrent failures share one refresh), and stops with `auth.dead`
when the cookie itself expires.

* Load it from the environment or a secret manager: `GABRIEL_COOKIE_D` (and optionally
  `GABRIEL_WORKSPACE`).
* For local development, `gabriel auth parse-curl --save` stores it in
  `~/.config/gabriel/auth.json` (mode 0600, shared with the Node CLI); `load_session_cookie()`
  reads the environment first, then that file.
* Use `allowed_teams` to refuse connecting to any other workspace.

The SDK never logs, emits or returns tokens or cookies; reprs of credential objects are redacted.

## Quick start

```python
import asyncio
from gabriel import GabrielClient, MessageCreatedEvent, load_session_cookie

async def main() -> None:
    session = load_session_cookie()
    client = GabrielClient.from_cookie(
        cookie_d=session.cookie_d, workspace=session.workspace, allowed_teams=["T0123456789"]
    )

    @client.on_message(mentions_me=True)
    async def greet(evt: MessageCreatedEvent, ctx) -> None:
        if evt.origin != "other_user":
            return  # skip bots and your own messages typed elsewhere
        await ctx.reply(f"Hi {evt.sender.display_name or 'there'} 👋")

    @client.on("auth.dead")
    def dead(evt, ctx) -> None:
        alert(f"Gabriel session expired: {evt.reason}")

    client.on_error(lambda err, evt: print(evt and evt.event_id, err))

    async with client:            # start() … stop(drain=10.0)
        await asyncio.Event().wait()

asyncio.run(main())
```

More in [`examples/`](https://github.com/Easy-Ez/gabriel-slack/tree/main/sdks/python/examples): a streamed reply to mentions, a read-only live smoke test and
raw frames. Run one with `GABRIEL_COOKIE_D=… uv run python examples/mention_stream_reply.py`.

## Events

Subscribe with `client.on(type_or_class, handler)`, `client.on_message(matcher, handler)`,
`client.on_thread_reply(handler)` — each also works as a decorator — or pull with
`async for evt in client.events()`. Handlers receive `(evt, ctx)` and may be sync or async.

Every event is a dataclass with snake_case attributes mapping 1:1 to the schema's camelCase
fields; `evt.to_dict()` / `evt.to_json()` produce the schema form. Select events by type string
(`"message.created"`) or by class (`MessageCreatedEvent`).

| Event | Class | When |
| :--- | :--- | :--- |
| `message.created` | `MessageCreatedEvent` | Any new message: channel, thread reply, broadcast, file share, bot. |
| `message.updated` | `MessageUpdatedEvent` | The body actually changed (metadata-only updates are filtered). |
| `message.deleted` | `MessageDeletedEvent` | A message was deleted (`deleted_ts`). |
| `message.thread_meta_updated` | `MessageThreadMetaUpdatedEvent` | A thread's reply count / latest reply changed. |
| `message.other` | `MessageOtherEvent` | Message subtypes without a dedicated model. |
| `ai.stream_updated` / `ai.stream_completed` | `AiStreamUpdatedEvent` / `AiStreamCompletedEvent` | Another AI app streams / finishes a reply. |
| `presence.changed` | `PresenceChangedEvent` | A watched user switched `active` ⇄ `away` (`is_initial_snapshot` for the first value). |
| `user.status_changed` / `user.dnd_changed` | `UserStatusChangedEvent` / `UserDndChangedEvent` | A watched user's custom status / DND changed. |
| `user.subteam_updated` | `UserSubteamUpdatedEvent` | A user group's members changed. |
| `interaction.typing` | `TypingEvent` | Someone is typing. |
| `interaction.reaction_added` / `_removed` | `ReactionAddedEvent` / `ReactionRemovedEvent` | Reaction on a message (`target.thread_root_ts` resolved) or file. |
| `channel.lifecycle` | `ChannelLifecycleEvent` | Joined / left / DM opened / renamed / archived. |
| `channel.marked` | `ChannelMarkedEvent` | Your read cursor moved (the SDK's own cursor changes are hidden). |
| `unread.activity` / `unread.badge_counts` | `UnreadActivityEvent` / `UnreadBadgeCountsEvent` | Activity item / badge snapshot (deduplicated). |
| `connection.ready` / `.reconnecting` / `.gap` | `Connection*Event` | Connection lifecycle and backfill reports. |
| `auth.refreshed` / `auth.dead` | `AuthRefreshedEvent` / `AuthDeadEvent` | Token refreshed / session expired (terminal). |
| `guard.loop_detected` / `.protocol_drift` / `.overflow` | `LoopDetectedEvent` / `ProtocolDriftEvent` / `OverflowEvent` | Circuit breaker / unknown frame / event dropped under load. |

Key fields of `MessageCreatedEvent`: `ts`, `channel` (`id`, `name`, `is_private`, `is_im`,
`is_mpim`), `sender` (`id`, `display_name`, `handle`, `is_bot`, `is_external`), `origin`, `text`
(raw mrkdwn), `markdown` (standard Markdown, mentions rendered as `@Name`), `thread`
(`root_ts`, `is_reply`, `broadcast` or `None`), `is_thread_reply`, `thread_ts`, `mentions`,
`is_mentioning_me`, `files`, `delivery` (`realtime` | `backfill`), `raw`.

**`origin`** distinguishes `this_client` (an echo of this SDK instance's own writes, dropped
unless `LoopGuardOptions(include_self_messages=True)`), `self_other_client`, `other_user` and `bot`.

## Handler context

```python
@client.on(MessageCreatedEvent)
async def handle(evt, ctx):
    ctx.signal                            # AbortSignal: aborted on delete, timeout or stop()
    await ctx.reply("text")               # in the trigger's thread (in_thread=False for top level)
    stream = await ctx.reply_stream()     # streaming reply in the trigger's thread
    await ctx.react("eyes")
    answer = await ctx.wait_for_reply(lambda r: r.thread_ts == evt.ts, timeout=60)  # None on timeout
    await ctx.hydrate_remaining()         # finish name resolution if the 300 ms budget ran out
```

`await ctx.signal.wait()` blocks until the signal fires; `ctx.signal.aborted` checks it.
`wait_for_reply` never misses a reply that arrived before it was called, releases the handler's
concurrency slot while waiting, and (with `consume=True`, the default) keeps the reply from
reaching other handlers. Handlers for one channel run one at a time in arrival order
(`ConcurrencyOptions(executor_key="thread")` switches to per-thread). Exceptions go to
`client.on_error` and never block later events. Middleware:
`client.use(async def mw(evt, ctx, next): …; await next())`.

## Actions

| Namespace | Methods |
| :--- | :--- |
| `client.chat` | `send(channel, markdown, *, blocks, thread_ts, broadcast, priority, preserve_unread_cursor, auto_chunk)`, `update`, `delete`, `send_typing`, `create_stream`, `stream` (async context manager) |
| `client.threads` | `get_context(channel, thread_ts, limit=50)` → messages, participants, `formatted_transcript`; `iterate_replies`, `mark_read`, `subscribe`, `unsubscribe` |
| `client.files` | `upload(filename, content, …)` (3-step, streaming), `download_stream` / `download_bytes` (`max_bytes`, sign-in page detection), `get_info`, `delete` |
| `client.search` | `messages`, `files`, `channels`, `people`, `iterate_messages`, `iterate_files` |
| `client.presence` | `watch(user_ids, track_profile_and_dnd=False)` → handle (`await h.unsubscribe()` / `async with`), `get_status(user_id)` |
| `client.reactions` | `add(channel, ts, emoji)`, `remove` (idempotent) |
| `client.users` | `resolve(id or ids)` (batched, cached), `get_dnd` |
| `client.channels` | `resolve`, `get_history`, `iterate_history` |

Streams: `append()` is synchronous; at most one update per stream is in flight and the final
message equals the buffer at `finish()`. Deleting the message in Slack aborts `stream.signal`.
Long output continues in a new message (`continuation_ts`).

```python
async with client.chat.stream("C0123456789", thread_ts=ts) as s:   # finish on exit, abort on error
    async for chunk in llm.stream(prompt):
        s.append(chunk)
```

Sending behavior (chunking at 11,500 effective characters, `client_msg_id` idempotency, per-channel
FIFO, a reserved slot for interactive replies, `preserve_unread_cursor`) and client-side limits
(presence frame ≤ 16,000 bytes, profile/DND ≤ 900 users) are identical to the Node SDK.

## AI safety

```python
from gabriel import ReadPolicy, ScopePolicy, WritePolicy

agent = client.scope(ScopePolicy(
    read=ReadPolicy(channels=["C0123456789"], allow_search=False, max_file_download_bytes=5 * 1024 * 1024),
    write=WritePolicy(
        target_scope="trigger_thread_only",
        allowed_actions=["chat.send", "chat.create_stream", "reactions.add"],
        disclosure_suffix="\n_— generated by an AI assistant_",
    ),
    approval=ask_a_human,   # async (action, params) -> bool
))
```

The scoped view has the same API. Anything outside the policy raises `SCOPE_POLICY_VIOLATION`
and is logged. Give LLM tools the scoped view, not the client.

`evt.to_llm_context()` renders metadata separately from content and wraps the content in
`<slack_message untrusted="true">`. Tell the model that `<content>` is data, not instructions.

## Reliability

* **Ordering**: per-channel dispatch in arrival order while entity lookups run in parallel.
* **De-duplication** by `event_id`.
* **Recovery**: after a cold reconnect, or a restart with a persistent store, missed channel
  messages and watched-thread replies are delivered as `delivery="backfill"` before newer realtime
  messages, followed by a `connection.gap` report.
* **State stores**: `MemoryStateStore` (default) and `FileStateStore(dir)` (single host; same file
  format as the Node SDK). Implement the `StateStore` protocol for other backends.
* **Shutdown**: `await client.stop(drain=10.0)` (or leaving `async with`) drains handlers, interrupts
  open streams, flushes sends and persists state.
* **Health**: `client.health()` → `HealthReport(status, ws_state, auth_state, last_frame_age, queue_depth)`.

## Errors

Every API raises `GabrielError` with `code`, `retryable`, `raw_slack_error` and `retry_after`
(seconds). Retryable errors were already retried — don't add another retry loop.

```python
from gabriel import GabrielError

try:
    await agent.chat.send(channel, markdown, thread_ts=ts)
except GabrielError as err:
    if err.code == "SCOPE_POLICY_VIOLATION":
        return "not allowed"
    raise
```

Codes: see [`spec/behavior.md` §12](https://github.com/Easy-Ez/gabriel-slack/blob/main/spec/behavior.md#12-errors).

## Low-level core

```python
client.core.on_event("ai_assistant_status", lambda frame: ...)   # unmodified frames
client.core.on_message_subtype("channel_topic", lambda frame: ...)
await client.core.call_web_api("reminders.add", {"text": "Review", "time": "in 30 minutes"})
await client.core.call_edge_api("users/search", {"query": "alice", "count": 5})
await client.core.send_ws_command({"type": "tickle", "reason": "keepalive"})
```

`GabrielCore` also works on its own (`async with GabrielCore(auth=AuthOptions(cookie_d=…)) as core:`).
It bypasses loop guards, origin detection and the sandbox. Never send `presence_sub` yourself.

## Configuration reference

```python
GabrielClient(
    auth=AuthOptions(cookie_d=..., token=None, workspace=None, allowed_teams=None),
    concurrency=ConcurrencyOptions(global_concurrency=32, per_key_concurrency=1, max_queue=5000,
                                   max_inflight_events=10_000, executor_key="channel", handler_timeout=300.0),
    hydration=HydrationOptions(hydrate_budget=0.3, preload_usergroups=True),
    loop_guard=LoopGuardOptions(include_self_messages=False, max_auto_replies_per_minute=5),
    chat=ChatSchedulerOptions(global_send_concurrency=4, item_deadline=30.0, stream_updates_per_second=2,
                              stream_min_interval=0.5, interrupted_notice="\n\n_(interrupted)_"),
    recovery=RecoveryOptions(max_pages_per_channel=10, watched_threads_limit=100, resync_buffer_limit=10_000),
    state_store=FileStateStore("/var/lib/my-agent/gabriel"),
    logger=console_logger("info"),       # default: stdlib logging, logger name "gabriel"
    metrics=my_metrics,                  # increment(name, labels) / observe(name, value, labels)
    advanced=AdvancedOptions(http_client=httpx.AsyncClient(proxy=...), ws_factory=..., metadata={"user_agent": ...},
                             endpoints=..., gateway_server=None, start_timeout=60.0),
)
```

`GabrielClient.from_cookie(cookie_d=…, workspace=…, allowed_teams=…, **options)` takes the same
options as keyword arguments.

## CLI

```bash
gabriel auth login your-workspace            # sign in in a dedicated Chrome window; saved with mode 0600
gabriel auth extract --port 9222             # read from a Chrome started with --remote-debugging-port
pbpaste | gabriel auth parse-curl --save     # "Copy as cURL" of any request from your signed-in Slack tab
gabriel auth status                          # verify and list workspaces (secrets redacted)
gabriel auth logout --yes
```

`python -m gabriel …` works too. The session file is shared with the Node CLI.

## Python conventions

* Durations are **float seconds** (`handler_timeout=300.0`, `retry_after`), except event payload
  fields that the schema defines in milliseconds (`ConnectionReconnectingEvent.delay_ms`,
  `AuthRefreshedEvent.refreshed_at`).
* Cancellation: methods accept `signal=AbortSignal`; ordinary `asyncio` task cancellation works too.
  SDK-initiated cancellation surfaces as `GabrielError("CANCELLED")`.
* Logging: any object with `debug/info/warn/error(fields, msg)`; `stdlib_logger()` adapts `logging`.

## Development

```bash
cd sdks/python
uv sync
uv run pytest            # unit, fake-Slack integration, conformance and schema tests
uv run mypy              # strict
uv run ruff check src tests examples && uv run ruff format --check src tests examples
```

Tests never touch a real workspace: [`tests/harness.py`](https://github.com/Easy-Ez/gabriel-slack/blob/main/sdks/python/tests/harness.py) is an in-memory Slack
(Web API, EdgeAPI, cookie exchange, WebSocket gateway).

## License

MIT © 2026 EzEasy
