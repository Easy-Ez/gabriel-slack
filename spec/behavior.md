# Gabriel SDK Behavior Specification

Status: **v0.1 (draft)** — normative for all Gabriel SDKs. "MUST", "SHOULD" and "MAY"
follow RFC 2119. Numbers in this document were measured against the Slack Web client
protocol; SDKs MUST use them as defaults and MAY expose them as options.

## 1. Scope

Gabriel speaks the protocol of the Slack **web client**: a workspace session token
(`xoxc-…`) paired with the `d` session cookie (`xoxd-…`). It does not use OAuth apps,
bot tokens or Events API webhooks. Because a session acts as the signed-in person, every
SDK MUST treat credentials as secrets and MUST make loop protection and least-privilege
sandboxing available by default (§8, §9).

## 2. Layers

| Layer | Name | Responsibility |
| :--- | :--- | :--- |
| Low | `GabrielCore` | Credentials, HTTP / EdgeAPI / WebSocket transport, connection state machine, raw frames. No business assumptions; frames are delivered unmodified. |
| High | `GabrielClient` | Typed events, entity hydration, ordering, loop guard, subscriptions, sending, recovery, sandbox. Built only on the low layer, which stays reachable as `client.core`. |

## 3. Credentials

1. The `d` cookie is the only required credential. The SDK MUST derive the workspace
   token, team ID, domain and user ID with `GET https://app.slack.com/auth?app=client&response_type=json`
   sending only `Cookie: d=…` and a current browser `User-Agent`.
2. When the session spans several workspaces the caller selects one (`workspace` =
   domain or team ID). An optional `allowedTeams` list MUST be enforced before connecting.
3. Auth state machine: `VALID → REFRESHING → VALID | AUTH_DEAD`.
   * `invalid_auth`, `not_authed`, `token_expired`, `token_revoked` trigger a refresh.
   * Concurrent failures MUST collapse into **one** exchange (singleflight); the failed
     request is replayed once with the new token.
   * An exchange that redirects, returns 401/403, non-JSON, or no longer lists the team
     means the cookie is dead: enter terminal `AUTH_DEAD`, stop reconnecting and retrying,
     and emit `auth.dead`.
   * Successful refreshes emit `auth.refreshed { teamId, trigger, refreshedAt }`.
4. Events, logs and errors MUST NOT contain token, cookie or reconnect-ticket values.
   Redacted form: prefix + `...[REDACTED, len=N]`.
5. SDKs MUST NOT offer a hook that hands refreshed tokens to application code.

## 4. HTTP

* Web API: `POST https://{domain}.slack.com/api/{method}`, `multipart/form-data`, fields
  `token`, `_x_reason`, `_x_mode=online`, `_x_sonic=true`, `_x_app_name=client`; objects are
  JSON-encoded. Default timeout **15 s**.
* EdgeAPI: `POST https://edgeapi.slack.com/cache/{teamId}/{endpoint}`, JSON body including
  `token`. Default timeout **10 s**. Batch lookups carry at most **100** IDs.
* HTTP 429: honor `Retry-After`; the cool-down is shared by every caller of the same method.
* Retries for timeouts / 5xx are allowed only for read-only methods, `chat.postMessage`
  with a `client_msg_id`, and overwrite-style writes (`chat.update`, `reactions.*`,
  `conversations.mark`, `subscriptions.thread.*`). Other writes MUST NOT be retried.
* Multipart encoding normalizes line breaks to CRLF; length budgets count `\n` as 2.
* Session credentials MUST only be sent to `https` Slack hosts.

## 5. WebSocket

### 5.1 Handshake

`wss://wss-primary.slack.com/?token=…&sync_desync=1&slack_client=desktop&start_args=…&no_query_on_subscribe=1&flannel=3&lazy_channels=1&gateway_server={teamId}-1&batch_presence_aware=1`
with headers `Cookie: d=…`, `Origin: https://app.slack.com`, `User-Agent: <browser>`.

### 5.2 State machine

```
DISCONNECTED → CONNECTING_FAST | CONNECTING_COLD → AWAITING_HELLO → RESYNCING → READY | READY_DEGRADED
any → BACKOFF → DISCONNECTED            any → CREDENTIAL_REFRESH → DISCONNECTED | AUTH_DEAD
```

* Each socket gets a new **connection epoch**; callbacks from older epochs are ignored.
* `hello` MUST arrive within **10 s** of open.
* Send `{"type":"ping","id":n}` every **10 s**. If no frame of any kind arrives for
  **25 s**, the peer is dead: close and reconnect.
* Upstream `id`s start at **16385** and increase monotonically; `ping` shares the counter.
  Commands awaiting `reply_to` time out after **10 s**; closing the socket MUST reject all
  pending commands immediately (`SOCKET_CLOSED`).
* `reconnect_url` frames carry a fast-reconnect ticket valid for **285 s** after receipt,
  and only for the token it was issued with. A fast attempt that fails before `hello`
  discards the ticket (the next attempt is cold).
* `goodbye`: close with code **4104** and reconnect immediately.
* Error frames: `Socket URL has expired` / `Subscription failure from the CS` discard the
  ticket; `invalid_auth` triggers a credential refresh; `rate_limited`, `ratelimited`,
  `backend_overloaded`, `unknown` delay the next attempt by `retry_after` (default 30 s).
* Backoff: full jitter, base **1 s**, cap **60 s**, reset after reaching READY.
* `hello.fast_reconnect === true` means the gateway replays frames missed while
  disconnected (measured up to 150 s). Cold connections replay nothing. **Neither keeps
  subscriptions**: all subscriptions MUST be replayed after every `hello`.

## 6. Events

### 6.1 One primary event per frame

| Raw frame | Event |
| :--- | :--- |
| `message` with no subtype, `file_share`, `thread_broadcast`, `bot_message`, `me_message` | `message.created` |
| `message_changed` with `streaming_state` (or `ai_context.result_status`) | `ai.stream_updated` / `ai.stream_completed` |
| `message_changed` otherwise, if the body hash (text + blocks + file IDs + attachments) changed | `message.updated` (metadata-only changes produce nothing) |
| `message_deleted` (target ts = `deleted_ts`) | `message.deleted` |
| `message_replied` | `message.thread_meta_updated` |
| any other `message` subtype | `message.other` |
| `presence_change`, `manual_presence_change` | `presence.changed` (after diffing, §7) |
| `user_change` / `user_invalidated` → refetch profile | `user.status_changed` (status actually changed) |
| `dnd_updated*` / `dnd_invalidated` → `dnd.teamInfo` | `user.dnd_changed` (DND actually changed) |
| `subteam_updated` / `subteam_members_changed` | `user.subteam_updated` |
| `user_typing` | `interaction.typing` |
| `reaction_added` / `reaction_removed` | `interaction.reaction_*` (target thread root resolved) |
| `channel_joined`, `channel_left`, `im_created`, `*_rename`, `*_archive`, self `member_joined/left_channel` | `channel.lifecycle` |
| `channel_marked`, `im_marked`, `group_marked`, `mpim_marked` | `channel.marked` (SDK-caused echoes suppressed) |
| `activity` | `unread.activity` |
| `badge_counts_updated` | `unread.badge_counts` (identical snapshots suppressed) |

Unknown frame types and message subtypes MUST be reported once via `guard.protocol_drift`
and stay available as raw frames.

### 6.2 Keys

* `eventId` — created: `{channel}:{ts}:created`; edits / deletes / AI streams:
  `{channel}:{targetTs}:{kind}:{eventTs}`; reactions add `:{user}:{reaction}`.
  Two edits of one message never share an `eventId`. SDKs de-duplicate message and
  reaction events by `eventId` (LRU ≥ 10,000).
* `laneKey` — from raw fields only: `channel ?? item.channel ?? subteam.id ?? user ?? "__global__"`.
* `executorKey` — `channel` (default) or `{channel}:{thread_ts ?? ts}` in thread mode.

### 6.3 Pipeline and ordering

1. Hydration (names, mentions, Markdown) starts immediately when a frame arrives, with a
   **300 ms** budget; on timeout the event is delivered with what resolved, and unresolved
   mentions keep their raw `<@U…>` form.
2. **Dispatch order** per `laneKey` equals arrival order regardless of hydration timing.
3. Handlers run in a bounded executor: global concurrency **32**, per key **1**, queue
   **5000**, in-flight bound **10,000**. Excess events are dropped with `guard.overflow`
   and pin the recovery watermark so they are re-fetched later. The WebSocket reader
   never blocks.
4. Handler failures are isolated and reported to the error callback. Handler time limit
   **300 s**: then the handler's cancellation signal fires (`HANDLER_TIMEOUT`); after a
   **5 s** grace the slot is released and further writes through that context fail
   with `CANCELLED`.
5. A `message_deleted` frame cancels handlers triggered by that message **immediately**,
   bypassing all queues.
6. `waitForReply` matches after hydration and before the executor queue, first scanning
   queued and recently delivered (10 s) messages; a waiting handler releases its slots
   and resumes ahead of queued work.

### 6.4 Message rendering

`markdown` is produced from `blocks` when present (rich text sections, lists, quotes,
preformatted with language, headers, sections, context, tables), else from mrkdwn entities
(`<@U…>`, `<#C…|name>`, `<!here>`, `<!subteam^S…>`, `<url|label>`), plus legacy
attachments. `isMentioningMe` is true for a direct mention of the user, a mention of a user
group the user belongs to, or `@here` / `@channel` / `@everyone`.

`unread.badge_counts.mentionCount` = sum of `at_user`, `at_user_group`, `at_channel`,
`at_everyone`, `keyword`, `unjoined_channel_mention`, `list_record_assigned`,
`list_user_mentioned`, `list_todo_notification`, `list_approval_request`,
`list_approval_reviewed`. Unknown activity keys are passed through.

## 7. Subscriptions

| Pool | Frame | Semantics | Limit |
| :--- | :--- | :--- | :--- |
| Presence | `presence_sub { ids }` | **Full overwrite**, no `reply_to`; debounce 100 ms | Serialized frame ≤ **16,000 bytes** (gateway silently drops frames over 16,384 bytes, losing every subscription) |
| Profile + DND | `subscription_update` subtypes `user_change_(un)subscribe_request`, `dnd_invalidated_(un)subscribe_request` | Incremental, ACKed via `reply_to` | ≤ **100** IDs per frame, ≤ **900** IDs per connection; gateway code 12 = cap reached, 15 = retry after 10 s |

* Subscriptions are reference-counted handles; the last release unsubscribes.
* Exceeding a limit MUST fail synchronously with `SUBSCRIPTION_CAP_EXCEEDED`.
* The gateway pushes an initial snapshot for every newly subscribed user (active and away)
  per connection. SDKs emit `isInitialSnapshot: true` for the first value, synthesize a
  snapshot for users already known on the current connection, and after reconnects emit
  only users whose presence actually changed.

## 8. Origin and loop protection

* `origin`: `bot` (frame has `bot_id`), `other_user`, `this_client` (written by this SDK
  instance), `self_other_client` (the same account from elsewhere, including manual edits
  of SDK messages).
* Every write is registered **before** the request (`client_msg_id`, then `ts`, content
  hashes, active stream timestamps) so a fast echo cannot be misclassified.
* `this_client` echoes are not delivered unless explicitly requested.
* Automatic writes (made inside a handler) are limited to **5 per (channel, thread) per
  60 s**; beyond that writes fail with `LOOP_DETECTED` and `guard.loop_detected` fires.

## 9. Sending

* Markdown is sent as `markdown_text` (the `text` field is truncated at 4,000 characters).
* Server limits: ≤ **12,000** code points (`msg_too_long`) and ≤ ~12,000 characters after
  emoji expand to `:shortcode:` (`msg_blocks_too_long`). SDKs budget **11,500** effective
  characters (code points, newlines × 2, each emoji × 20) and split automatically,
  preferring blank lines → newlines → word boundaries outside code; code fences are closed
  and reopened, table headers repeated. On a length error, re-split at 0.8×.
* Same-channel posts are strictly FIFO; a global send semaphore (default 4) reserves one slot
  for interactive replies. Per-item deadline **30 s**.
* `chat.postMessage` is idempotent per `client_msg_id`; a WebSocket echo carrying the same
  `client_msg_id` also confirms the send.
* `chat.update` / `chat.delete` retry `message_not_found` every **175 ms** up to **10** times,
  only within **2 s** of the message's creation.
* Posting advances the channel's read cursor; editing does not. `preserveUnreadCursor`
  latches `last_read` before the first concurrent post, and after the last one restores it
  with `conversations.mark` — only if the channel had unreads and the user did not read it
  meanwhile. Best effort.
* Streaming (session tokens cannot use `chat.startStream`): post a placeholder, then
  `chat.update` with at most one request in flight per stream, latest content wins, shared
  workspace rate. Intermediate updates close open code fences on a copy; past the budget the
  message is finalized and continued in a new message. Deleting a streamed message cancels
  the stream.

## 10. Recovery

* Watermarks per channel (and per watched thread) advance only on `message.created`, only up
  to the newest timestamp with nothing older still in flight or dropped; persisted with
  monotonic max-merge.
* Cold reconnect / restart with persisted state: buffer realtime frames, call `client.counts`,
  fetch `conversations.history(oldest = watermark)` for channels whose `latest` is newer
  (bounded pages), fetch `conversations.replies(oldest = threadWatermark)` for watched threads
  (thread replies never appear in channel history), deliver recovered messages with
  `delivery: "backfill"` in timestamp order before the buffered realtime frames, de-duplicate
  by `eventId`, and emit `connection.gap`.
* Fast reconnect: become ready immediately, then reconcile once after **2 s** the same way.
* `connection.gap.recovered === false` lists every unrecovered range with a reason.

## 11. Sandbox

`scope(policy)` returns a view with the same API where reads are limited to allow-listed
channels (history, thread context, file downloads, search hits), search can be disabled,
download size capped, writes limited to an action allow-list and — with
`trigger_thread_only` — to the channel and thread of the running handler's trigger, an
optional disclosure suffix is appended to written content, and an approval hook is consulted
for every write. Violations fail with `SCOPE_POLICY_VIOLATION` and are logged. Scoped views
never expose the low-level core.

`toLLMContext()` separates metadata from body and marks the body as untrusted; sequences that
could close the envelope are neutralized.

## 12. Errors

| Code | Retryable | Meaning |
| :--- | :---: | :--- |
| `RATE_LIMITED` | ✓ | 429 after retries |
| `NETWORK_TIMEOUT`, `SOCKET_CLOSED`, `SERVER_ERROR` | ✓ | transient transport failure after retries |
| `MESSAGE_NOT_FOUND_TRANSIENT`, `TOKEN_EXPIRED` | ✓ | handled internally |
| `AUTH_DEAD` | | root credential invalid |
| `NOT_IN_CHANNEL`, `CHANNEL_NOT_FOUND`, `PERMISSION_DENIED` | | access problems |
| `MSG_TOO_LONG`, `MSG_BLOCKS_TOO_LONG` | | only when auto-chunking is disabled |
| `SUBSCRIPTION_CAP_EXCEEDED`, `SCOPE_POLICY_VIOLATION`, `FILE_TOO_LARGE`, `LOOP_DETECTED` | | guard rejections |
| `HANDLER_TIMEOUT`, `CANCELLED`, `STREAM_CLOSED`, `QUEUE_OVERFLOW`, `STATE_STORE_UNAVAILABLE` | | lifecycle |
| `INVALID_ARGUMENT`, `INVALID_STATE`, `SLACK_API_ERROR` | | caller errors / unmapped Slack errors (`rawSlackError` holds the original) |

Retryable errors reaching the caller mean automatic retries were exhausted; callers SHOULD NOT
add another retry layer.
