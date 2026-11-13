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
| `message_changed` otherwise, if the body hash (text + blocks + file IDs + attachments) changed | `message.updated` (metadata-only changes produce nothing; a change that only adds or updates link previews still produces one, §6.5) |
| `message_deleted` (target ts = `deleted_ts`) | `message.deleted` |
| `message_replied` | `message.thread_meta_updated` |
| any other `message` subtype | `message.other` |
| `presence_change`, `manual_presence_change` | `presence.changed` (after diffing, §7) |
| `user_change` / `user_invalidated` / `user_status_changed` → refresh profile (§7.1) | `user.profile_changed` (display name, real name or handle actually changed), then `user.status_changed` (status actually changed, profile-subscribed users only) |
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
   **1000 ms** budget; on timeout the event is delivered with what resolved, and unresolved
   mentions keep their raw `<@U…>` form. An event that timed out is rendered again with the names
   known by then when it leaves its lane and when each handler starts.
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
attachments that are not link previews. Link previews are never part of `markdown` (§6.5).

`message.created.mentionReasons` lists why the message mentions the user, each reason at most
once and in this order:

| Reason | When |
| :--- | :--- |
| `direct` | a `user` mention whose ID is the signed-in user |
| `usergroup` | a `usergroup` mention of a group the user is known to belong to |
| `broadcast` | `@here`, `@channel` or `@everyone` |

It is empty when the message does not mention the user. Keyword highlights never count.
`isMentioningMe` is `mentionReasons.length > 0`, so it is also true for user groups and
broadcasts; consumers that only care about the user being named use
`mentionReasons.includes("direct")`.

A `usergroup` mention carries `includesMe` (`true` / `false`) when the client knows the user's
membership of that group (from the user group preload, `subteam_self_added` /
`subteam_self_removed`, or a `subteam_members_changed` that adds or removes the user). When the
membership is unknown, `includesMe` is absent and the group does not count as a `usergroup`
reason.

When the first block is an attribution block (§9.1), it is removed before rendering and
mention extraction, and exposed as `attribution: { text, review? }` on `message.created`,
`message.updated` (current content) and fetched thread / history messages. `text` is the
block rendered to Markdown; `review` is the block ID suffix when it is `confirmed`,
`unconfirmed` or `hidden`, and absent otherwise.

`unread.badge_counts.mentionCount` = sum of `at_user`, `at_user_group`, `at_channel`,
`at_everyone`, `keyword`, `unjoined_channel_mention`, `list_record_assigned`,
`list_user_mentioned`, `list_todo_notification`, `list_approval_request`,
`list_approval_reviewed`. Unknown activity keys are passed through.

### 6.5 Link previews

Slack puts what the sender wrote in `text` / `blocks` and the previews it generates for links
(unfurls) in `attachments`, often seconds later through a `message_changed`. Previews are given
apart from the body so that consumers can tell them from the sender's own words.

An attachment is a **link preview** when it has `original_url`, `from_url`, `is_app_unfurl` or
`is_msg_unfurl` (a shared Slack message). Other attachments (for example legacy attachments posted
by bots, which carry none of these) keep being rendered into `markdown` as before.

Each preview becomes `{ url, serviceName?, title?, text? }`, in attachment order:

| Field | Source |
| :--- | :--- |
| `url` | `original_url`, else `from_url`, else `app_unfurl_url`, else `title_link`; empty string when none is present |
| `serviceName` | `"Slack"` for a shared Slack message (`is_msg_unfurl`), else `service_name`; absent when missing (app unfurls such as GitHub's usually have none) |
| `title` | `title`, rendered like `markdown` (§6.4); absent when empty |
| `text` | the attachment `blocks` rendered like `markdown`, else `text` rendered the same way; absent when empty |

* `message.created.linkPreviews` and `message.updated.currentLinkPreviews` are always present
  (empty arrays when there is none); `markdown` / `currentMarkdown` / `previousMarkdown` never
  contain them.
* A `message_changed` whose only change is a new or updated preview still produces
  `message.updated`, with `currentMarkdown` equal to the previous body and the preview in
  `currentLinkPreviews`.
* History, thread, activity and search messages carry `linkPreviews` by the same rules (§10.1).
* `toLLMContext()` lists previews in their own section inside the untrusted envelope, marked as
  generated automatically and not written by the sender (§11).

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

### 7.1 Profile changes

* The gateway pushes `user_change` / `user_invalidated` only for users subscribed with
  `user_change_subscribe_request` on the current connection (measured on the official web client:
  every invalidation received was for a subscribed user; most frames are `user_invalidated` with
  only `user.id`). `watchProfiles(ids)` subscribes users for profile changes only; `watch(ids,
  { trackProfileAndDnd: true })` subscribes profile and DND. Both share the 900-ID pool; DND
  subscriptions are sent only for `trackProfileAndDnd` watchers.
* A frame whose `user` object has `name` and `profile` is used as the new record; any other frame
  re-fetches the user (EdgeAPI `users/info`). The user cache is updated **before** any event is
  emitted, so later events and `users.resolve()` see the new values.
* Frames are processed for subscribed users and for users that already have a cached record; other
  users are ignored (no lookup, no event).
* `user.profile_changed` is emitted when `displayName`, `realName` or `handle` differs from the
  previous record (a field missing from the new record never counts). `changed` lists the fields in
  that order and `previous` holds their old values. No previous record → no event. The previous
  record of a subscribed user is kept for as long as it is subscribed, independent of the cache TTL.
* Status-only and avatar-only changes never produce `user.profile_changed`.
* Processing is read-only: it never sends a write request.

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

### 9.1 Agent attribution

Messages written through a session token are indistinguishable from the account owner's own
messages. An optional attribution line marks them as written by an agent. It is off by
default and is a self-declared display hint: any client can write the same block.

* Configured once on the client and overridable per write: `false` disables it for one
  write, a partial preset is merged over a preset default (only `review` over a custom
  default), and a custom line replaces the default. Two forms:
  * **Preset** `{ icon, name, role, review }` renders
    `{icon} {name} ({role}){suffix}`. `icon` defaults to `:robot_face:` (`null` omits it) and
    must be an emoji shortcode; `name` is required; `role` is optional; `name` and `role`
    are trimmed, single-line, at most 64 characters, and escaped (`&`, `<`, `>`).
  * **Custom** `{ text }`: mrkdwn written as given (at most 3,000 effective characters as
    measured in §9); `{self}` is replaced with `<@SELF>`. `text` may be a function of
    `{ selfUserId, channel, threadTs, review }`.
* `review` suffixes, where `<@SELF>` is the sending account:

  | `review` | Suffix |
  | :--- | :--- |
  | `confirmed` | ` · confirmed by <@SELF>` |
  | `unconfirmed` (default) | ` · sent for <@SELF> · not reviewed` |
  | `hidden` | none |

  `confirmed` is accepted only per write, never as the client default: the SDK cannot know
  that a person reviewed a message, so the caller must say so for each one.
* Both forms MUST reject `<!channel>`, `<!here>`, `<!everyone>` and `<!subteam^…>` with
  `INVALID_ARGUMENT`; client options are validated when the client is created. Scoped views
  (§11) MUST reject per-write overrides with `SCOPE_POLICY_VIOLATION`, so a tool cannot
  claim `confirmed` or drop the line.
* Wire format: `blocks` = `[attribution, body]`, where attribution is
  `{ type: "context", block_id: "gabriel.attribution.<review|custom>", elements: [{ type:
  "mrkdwn", text }] }` and body is `{ type: "markdown", text: <markdown> }` (Slack stores it
  as the same `rich_text` / `table` blocks as `markdown_text`). `text` carries a plain-text
  fallback of the body (at most 3,000 characters). Caller-supplied `blocks` get the
  attribution block prepended.
* Every write carries the block: each auto-chunked message, `chat.update`, and every stream
  placeholder, refresh and continuation (an update without it would remove the line).
  Chunk and stream budgets are reduced by the attribution's effective length.
* SDKs expose a pure renderer and `chat.previewAttribution(override?)` returning the exact
  `{ text, block }` that would be written (or `null` when disabled).

## 10. Recovery

* Watermarks per channel (and per watched thread) advance only on `message.created`, only up
  to the newest timestamp with nothing older still in flight or dropped; persisted with
  monotonic max-merge.
* Cold reconnect / restart with persisted state: buffer realtime frames, call `client.counts`,
  fetch `conversations.history(oldest = watermark)` for channels whose `latest` is newer
  (bounded pages), fetch `conversations.replies(oldest = threadWatermark)` for watched threads
  (thread replies never appear in channel history), look up the names of all recovered messages
  together (waiting at most **5 s**, independent of the per-event budget), deliver recovered
  messages with `delivery: "backfill"` in timestamp order before the buffered realtime frames, de-duplicate
  by `eventId`, and emit `connection.gap`.
* Fast reconnect: become ready immediately, then reconcile once after **2 s** the same way.
* `connection.gap.recovered === false` lists every unrecovered range with a reason.

### 10.1 Reading past messages

Besides history and thread replies, the SDK reads three client endpoints. None of them marks
anything read, opens or closes a conversation, or writes in any other way.

* Direct conversations: `client.dms` (`count` ≤ 250, `include_closed`, `include_channel=true`,
  `exclude_bots=false`, `cursor`). Each entry yields the conversation (`isIm` / `isMpim`, the peer
  of a 1:1 DM), the timestamp of its newest message when reported, and whether it is open.
* Activity feed: `activity.feed` (`types` comma-separated, `limit` ≤ 50, `cursor`,
  `mode=chrono_reads_and_unreads`, or `chrono_unreads` with `unread_only=true`). Entries carry a
  key, a type (`at_user`, `at_user_group`, `at_channel`, `at_everyone`, `keyword`, `dm`,
  `thread_v2`, `message_reaction`, …), a feed timestamp, an unread flag and at most a pointer to a
  message (channel, ts, thread ts), never its content. Iteration is newest first, de-duplicated by
  key, and stops at the first entry older than the requested lower bound.
* Messages by timestamp: `messages.list` (`message_ids = [{ channel, timestamps }]`,
  `org_wide_aware=true`), at most **50** messages and **20** channels per call (more channels
  fail with `too_many_channels`). Activity entries are resolved to
  full messages this way; an entry whose message no longer exists is kept without a message.

History, thread, activity and search messages carry `mentions` and `mentionReasons` computed by
the rules of §6.4, and `linkPreviews` by the rules of §6.5 (their `markdown` excludes previews). History messages also carry the raw `subtype` and a deletion flag.

## 11. Sandbox

`scope(policy)` returns a view with the same API where reads are limited to allow-listed
channels (history, thread context, messages by timestamp, direct conversations, activity
entries, file downloads, search hits), search can be disabled,
download size capped, writes limited to an action allow-list and — with
`trigger_thread_only` — to the channel and thread of the running handler's trigger, an
optional disclosure suffix is appended to written content, and an approval hook is consulted
for every write. Violations fail with `SCOPE_POLICY_VIOLATION` and are logged. Scoped views
never expose the low-level core.

`toLLMContext()` separates metadata from body and marks the body as untrusted; sequences that
could close the envelope are neutralized. Link previews (§6.5) follow the body in their own
`<link_previews>` section, still inside the envelope, labelled as generated automatically and not
written by the sender.

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
