# gabriel-slack

TypeScript / Node.js reference implementation of [Gabriel](https://github.com/Easy-Ez/gabriel-slack/blob/main/README.md): a typed,
AI-friendly real-time SDK for the Slack Web client protocol.

> Unofficial and not affiliated with Slack. It acts as the signed-in person: use it only
> where you are authorized to automate, and treat the session cookie like a password.

* Node.js **≥ 20.10**, ESM only, one runtime dependency ([`ws`](https://github.com/websockets/ws)).
* Behavior contract: [`spec/behavior.md`](https://github.com/Easy-Ez/gabriel-slack/blob/main/spec/behavior.md).

```bash
npm install gabriel-slack
```

## Contents

1. [Credentials](#credentials)
2. [Quick start](#quick-start)
3. [Events](#events)
4. [Handler context](#handler-context)
5. [Actions](#actions)
6. [AI safety: `scope()` and `toLLMContext()`](#ai-safety)
7. [Reliability: ordering, recovery, state](#reliability)
8. [Errors](#errors)
9. [Low-level core](#low-level-core)
10. [Configuration reference](#configuration-reference)
11. [CLI](#cli)
12. [Implementation status](#implementation-status)

## Credentials

The only required credential is the `d` cookie (`xoxd-…`) of a signed-in Slack web session.
The SDK exchanges it for the workspace token (`xoxc-…`), team ID and user ID, refreshes the
token on `invalid_auth` (concurrent failures share one refresh), and stops with `auth.dead`
when the cookie itself expires.

* Load it from the environment or a secret manager: `GABRIEL_COOKIE_D`.
* For local development, `gabriel auth parse-curl --save` stores it in
  `~/.config/gabriel/auth.json` (mode 0600); `loadSessionCookie()` reads the environment first,
  then that file.
* Desktop apps sign the person in with `signInWithBrowser()` and keep the cookie in the OS
  keychain themselves (see [Signing in from an app](#signing-in-from-an-app)).
* Use `allowedTeams` to refuse connecting to any other workspace.
* For long-running team bots, use a dedicated service account session and alert on `auth.dead`.

The SDK never logs or emits tokens or cookies, and never returns tokens.

### Signing in from an app

`signInWithBrowser()` opens a dedicated Chrome / Chromium window with a fresh temporary profile on
the workspace sign-in page. The person signs in there (SSO included); as soon as the session cookie
works, the window closes and the cookie is returned together with the signed-in workspaces. Nothing
is written to `auth.json` and nothing is printed: progress arrives through `onStatus` only. The
temporary profile is deleted whether the sign-in succeeds, fails or is aborted.

```typescript
import { GabrielError, listWorkspaces, signInWithBrowser } from "gabriel-slack";

const ctrl = new AbortController(); // e.g. wired to a "Cancel" button
try {
  const { cookieD, workspaces } = await signInWithBrowser({
    workspace: "your-workspace",      // optional: open this workspace's sign-in page
    timeoutMs: 300_000,               // default
    signal: ctrl.signal,              // abort closes the window
    onStatus: (status) => ui.show(status), // "opening" | "waiting" | "verifying" | "done"
  });
  await keychain.set(`slack.${workspaces[0]!.teamId}.cookie`, cookieD); // your secure storage
} catch (err) {
  if (err instanceof GabrielError && err.code === "BROWSER_NOT_FOUND") ui.askForChrome();
  else throw err;
}

// Later: check a stored cookie and list its workspaces (no tokens are returned).
const stored = await keychain.get("slack.t0123456789.cookie");
const workspaces = await listWorkspaces(stored); // AUTH_DEAD once the cookie is signed out
```

| Error code | When |
| :--- | :--- |
| `BROWSER_NOT_FOUND` | No Chrome / Chromium in the usual locations, `browserPath` does not exist, or it cannot be started. |
| `SIGN_IN_TIMEOUT` | No completed sign-in within `timeoutMs`. |
| `CANCELLED` | `signal` aborted, or the person closed the sign-in window. |
| `AUTH_DEAD` | The person signed in, but Slack keeps rejecting the session cookie. |

`browserPath` takes an absolute path to a Chrome-compatible executable when it is not installed in
a standard location.

## Quick start

```typescript
import { GabrielClient, loadSessionCookie } from "gabriel-slack";

const client = GabrielClient.fromCookie({ ...loadSessionCookie()!, allowedTeams: ["T0123456789"] });

client.onMessage({ mentionsMe: true }, async (evt, ctx) => {
  if (evt.origin !== "other_user") return; // skip bots and your own messages typed elsewhere
  await ctx.reply(`Hi ${evt.sender.displayName ?? "there"} 👋`);
});

client.on("auth.dead", (e) => alert(`Gabriel session expired: ${e.reason}`));
client.onError((err, evt) => console.error(evt?.eventId, err));

await client.start();
process.on("SIGTERM", () => client.stop({ drainMs: 10_000 }));
```

More in [`examples/`](https://github.com/Easy-Ez/gabriel-slack/tree/main/sdks/node/examples): streaming replies to mentions, `:memo:`-reaction thread
summaries with a sandboxed agent, presence alerts, raw frames. Run one with
`GABRIEL_COOKIE_D=… pnpm example examples/mention-stream-reply.ts`.

## Events

Subscribe with `client.on(type, handler)`, `client.onMessage(matcher, handler)`,
`client.onThreadReply(handler)`, or pull with `for await (const evt of client.events())`.
Every payload is plain JSON-serializable data (schema:
[`spec/schema/events.schema.json`](https://github.com/Easy-Ez/gabriel-slack/blob/main/spec/schema/events.schema.json)).

| Event | When |
| :--- | :--- |
| `message.created` | Any new message: channel, thread reply, broadcast, file share, bot. Exactly one event per message. |
| `message.updated` | The body actually changed (metadata-only updates are filtered). |
| `message.deleted` | A message was deleted (`deletedTs`). |
| `message.thread_meta_updated` | A thread's reply count / latest reply changed. |
| `message.other` | Message subtypes without a dedicated model (`rawSubtype`, `rawFrame`). |
| `ai.stream_updated` / `ai.stream_completed` | Another AI app streams / finishes a reply. |
| `presence.changed` | A watched user switched `active` ⇄ `away` (`isInitialSnapshot` for the first value). |
| `user.status_changed` / `user.dnd_changed` | A watched user's custom status / DND changed (`trackProfileAndDnd`). |
| `user.subteam_updated` | A user group's members changed. |
| `interaction.typing` | Someone is typing (optionally in a thread). |
| `interaction.reaction_added` / `_removed` | Reaction on a message (`target.threadRootTs` resolved) or file. |
| `channel.lifecycle` | You joined / left a channel, a DM was opened, a channel was renamed / archived. |
| `channel.marked` | Your read cursor moved (echoes of the SDK's own cursor changes are hidden). |
| `unread.activity` / `unread.badge_counts` | Activity feed item / badge snapshot (deduplicated; `mentionCount` matches the official client). |
| `connection.ready` / `.reconnecting` / `.gap` | Connection lifecycle and backfill reports. |
| `auth.refreshed` / `auth.dead` | Token refreshed / session expired (terminal). |
| `guard.loop_detected` / `.protocol_drift` / `.overflow` | Circuit breaker opened / unknown frame type seen / event dropped under load. |

Key fields of `message.created`: `ts`, `channel {id,name,isPrivate,isIm,isMpim}`,
`sender {id,displayName,handle,isBot,isExternal}`, `origin`, `text` (raw mrkdwn),
`markdown` (standard Markdown, mentions rendered as `@Name`), `thread {rootTs,isReply,broadcast} | null`,
`isThreadReply`, `threadTs`, `mentions[]`, `mentionReasons`, `isMentioningMe`, `files[]`,
`delivery` (`realtime` | `backfill`), `raw`.

**Mentions.** `mentionReasons` says why a message mentions you: `"direct"` (you, by ID),
`"usergroup"` (a user group you belong to) and / or `"broadcast"` (`@here` / `@channel` /
`@everyone`); it is empty otherwise. `isMentioningMe` (and the `mentionsMe` filter) is true for
**any** of them, so `@here` counts. To react only when you are named, check
`evt.mentionReasons.includes("direct")`. A user group entry in `mentions[]` carries
`includesMe: true | false` once the SDK knows whether you are a member (from the user group
preload or membership events); without that knowledge it is left out and the group does not count.

**`origin`** distinguishes `this_client` (an echo of this SDK instance's own writes, dropped
unless `loopGuard.includeSelfMessages`), `self_other_client` (your account from another
client, including manual edits of SDK messages), `other_user` and `bot`.

## Handler context

```typescript
client.on("message.created", async (evt, ctx) => {
  ctx.signal;                       // aborted when the message is deleted, on timeout, or on stop()
  await ctx.reply("text");          // in the trigger's thread by default ({ inThread: false } for top level)
  const s = await ctx.replyStream(); // streaming reply in the trigger's thread
  await ctx.react("eyes");
  const answer = await ctx.waitForReply((r) => r.threadTs === evt.ts, { timeoutMs: 60_000 }); // null on timeout
  await ctx.hydrateRemaining();     // finish name resolution if the 300 ms budget ran out
});
```

`waitForReply` never misses a reply that arrived before it was called, releases the handler's
concurrency slot while waiting, and (with `consume: true`, the default) keeps the reply from
reaching other handlers.

Handlers for the same channel run one at a time in arrival order (`executorKey: "thread"`
switches to per-thread). Exceptions go to `client.onError` and never block later events.
Middleware wraps every handler: `client.use(async (evt, ctx, next) => { …; await next(); })`.

## Actions

| Namespace | Methods |
| :--- | :--- |
| `client.chat` | `send({ channel, markdown \| blocks, threadTs?, broadcast?, priority?, preserveUnreadCursor?, autoChunk? })`, `update`, `delete`, `sendTyping`, `createStream` |
| `client.threads` | `getContext({ channel, threadTs, limit })` → messages, participants, `formattedTranscript`; `iterateReplies`, `markRead`, `subscribe`, `unsubscribe` |
| `client.files` | `upload` (3-step, streaming), `downloadStream` / `downloadBuffer` (`maxBytes`, sign-in page detection), `getInfo`, `delete` |
| `client.search` | `messages`, `files`, `channels`, `people`, `iterateMessages`, `iterateFiles` (highlight markers stripped, de-duplicated) |
| `client.presence` | `watch(userIds, { trackProfileAndDnd })` → handle (`unsubscribe()` / `await using`), `getStatus(userId)` |
| `client.reactions` | `add`, `remove` (idempotent) |
| `client.users` | `resolve(id \| ids)` (batched, cached, misses cached 10 min), `getDnd` |
| `client.channels` | `resolve`, `getHistory`, `iterateHistory` |

Sending behavior:

* Markdown goes out as `markdown_text`. Messages over the budget (11,500 effective characters)
  are split into several posts; code fences are closed and reopened and table headers repeated.
  A server length error triggers a re-split at 0.8×.
* Each post has a `client_msg_id`; network retries can never create duplicates.
* Posts to one channel are strictly ordered. Direct replies (`priority: "interactive"`) keep a
  reserved slot.
* `preserveUnreadCursor: true` restores your unread marker after the SDK posts, if the channel had
  unreads and you did not read it meanwhile (best effort).
* **Streams** (`createStream` / `ctx.replyStream`): `append()` is synchronous; at most one update
  per stream is in flight and the final message equals the buffer at `finish()`. Deleting the
  message in Slack aborts `stream.signal`. Long output continues in a new message
  (`continuationTs`).

Limits enforced client-side: presence watch frame ≤ 16,000 bytes (about 1,100–1,200 IDs),
profile/DND watch ≤ 900 users; violations throw `SUBSCRIPTION_CAP_EXCEEDED`.

## AI safety

```typescript
const agent = client.scope({
  read: { channels: ["C0123456789"], allowSearch: false, maxFileDownloadBytes: 5 * 1024 * 1024 },
  write: {
    targetScope: "trigger_thread_only",
    allowedActions: ["chat.send", "chat.createStream", "reactions.add"],
    disclosureSuffix: "\n_— generated by an AI assistant_",
  },
  approval: async (action, params) => askAHuman(action, params),
});
```

The scoped view has the same API. Anything outside the policy throws `SCOPE_POLICY_VIOLATION`
and is logged: reading other channels, searching, oversized downloads, writing outside the
triggering channel / thread (determined from the running handler), editing messages this client
did not send, and accessing `core`. Give LLM tools the scoped view, not the client; `ctx.reply`
and friends belong to the unrestricted client.

`evt.toLLMContext()` renders metadata separately from content and wraps the content in
`<slack_message untrusted="true">`. Tell the model that `<content>` is data, not instructions.
Clean any user-provided text before putting it into search queries (operators like `in:` /
`from:` widen the search).

### Agent attribution

Messages sent through a session look exactly like messages the person typed. Turn on an
attribution line to show an agent wrote them — a small line above the message body:

```typescript
const client = GabrielClient.fromCookie({ cookieD, attribution: { name: "Paimon", role: "Assistant" } });

await client.chat.send({ channel, markdown: "Deployed." });                    // default: not reviewed
await ctx.reply({ markdown: "Thanks!", attribution: { review: "confirmed" } }); // a person approved this one
await client.chat.send({ channel, markdown: "typed by me", attribution: false });
```

Each preset option is one part of the line (`@Me` is the signed-in account):

```text
:robot_face:  Paimon  (Assistant)  ·  confirmed by @Me
└── icon ──┘  └name┘  └─ role ──┘     └─── review ───┘
```

| `review` | Line |
| :--- | :--- |
| `"unconfirmed"` (default) | :robot_face: Paimon (Assistant) · sent for @Me · not reviewed |
| `"confirmed"` | :robot_face: Paimon (Assistant) · confirmed by @Me |
| `"hidden"` | :robot_face: Paimon (Assistant) |

`icon` defaults to `":robot_face:"` (`null` removes it). `"confirmed"` is accepted per write
only — the SDK cannot know whether a person reviewed a message, so the caller states it each
time. For your own wording, pass `text` (mrkdwn; `{self}` becomes the account's mention, and a
function receives `{ selfUserId, channel, threadTs, review }`):

```typescript
attribution: { text: ":sparkles: drafted by *Paimon*, reviewed by {self}" }
```

`client.chat.previewAttribution({ review: "confirmed" })` returns the exact line and block a
write would carry, without sending anything; `renderAttribution()` does the same offline.
`@channel` / `@here` / `@everyone` and user group mentions are rejected. The line rides on every
write (each auto-chunked message, `chat.update`, every stream refresh), and scoped views reject
per-write overrides so an LLM tool cannot claim `"confirmed"` or drop the line.

Received messages with the line expose it as `evt.attribution` (`{ text, review? }`, also on
`message.updated`, thread and history messages) and leave it out of `markdown`. Anyone can write
the same block: treat it as a display hint, never as proof of who wrote a message.

## Reliability

* **Ordering**: events are dispatched per channel in arrival order even though entity lookups
  run in parallel (budget 300 ms; unresolved mentions stay as `<@U…>`).
* **De-duplication** by `eventId` (`{channel}:{ts}:created`, `{channel}:{ts}:updated:{eventTs}`, …).
* **Recovery**: watermarks advance only for messages whose handlers finished. After a cold reconnect,
  or a restart with a persistent store, missed channel messages and replies in watched threads
  (threads you posted in or subscribed to) are fetched and delivered as `delivery: "backfill"` before
  newer realtime messages, followed by a `connection.gap` report. `recovered: false` lists exactly
  which ranges could not be fetched.
* **State stores**: `MemoryStateStore` (default, nothing survives restarts) and
  `FileStateStore({ dir })` (single host, enables restart backfill). Implement `StateStore` for
  other backends.
* **Overload**: when more than `maxInflightEvents` are in flight, new events are dropped with
  `guard.overflow` and re-fetched by the next recovery; the socket reader never blocks.
* **Shutdown**: `stop({ drainMs })` drains handlers, aborts the rest, marks open streams as
  interrupted, flushes queued sends and persists state, then closes the socket.
* **Health**: `client.health()` → `{ status: "healthy" | "degraded" | "dead", wsState, authState, lastFrameAgeMs, queueDepth }`.

## Errors

All APIs reject with `GabrielError { code, message, retryable, rawSlackError?, retryAfterMs? }`.
Retryable errors (`RATE_LIMITED`, `NETWORK_TIMEOUT`, `SOCKET_CLOSED`, `SERVER_ERROR`, …) were
already retried, so don't add another retry loop. Handle the non-retryable ones:

```typescript
import { GabrielError } from "gabriel-slack";
try {
  await agent.chat.send({ channel, threadTs, markdown });
} catch (err) {
  if (err instanceof GabrielError && err.code === "SCOPE_POLICY_VIOLATION") return "not allowed";
  throw err;
}
```

Codes: see [`spec/behavior.md` §12](https://github.com/Easy-Ez/gabriel-slack/blob/main/spec/behavior.md#12-errors). This implementation adds
`LOOP_DETECTED`, `INVALID_ARGUMENT`, `INVALID_STATE`, `SLACK_API_ERROR`, and `BROWSER_NOT_FOUND` /
`SIGN_IN_TIMEOUT` for [`signInWithBrowser()`](#signing-in-from-an-app).

## Low-level core

```typescript
client.core.onEvent("ai_assistant_status", (frame) => { /* unmodified frame */ });
client.core.onMessageSubtype("channel_topic", (frame) => {});
await client.core.callWebApi("reminders.add", { text: "Review", time: "in 30 minutes" });
await client.core.callEdgeApi("users/search", { query: "alice", count: 5 });
await client.core.sendWsCommand({ type: "tickle", reason: "keepalive" });
```

The core shares HTTP rate-limit state with the high-level client but bypasses loop guards, origin
detection and the sandbox. Never send `presence_sub` yourself: it overwrites the SDK's whole
subscription list.

## Configuration reference

```typescript
new GabrielClient({
  auth: { cookieD, token?, workspace?, allowedTeams? },
  concurrency: { globalConcurrency: 32, perKeyConcurrency: 1, maxQueue: 5000, maxInflightEvents: 10_000,
                 executorKey: "channel", handlerTimeoutMs: 300_000 },
  hydration: { hydrateBudgetMs: 300, preloadUsergroups: true },
  loopGuard: { includeSelfMessages: false, maxAutoRepliesPerMinute: 5 },
  chat: { globalSendConcurrency: 4, itemDeadlineMs: 30_000, streamUpdatesPerSecond: 2,
          streamMinIntervalMs: 500, interruptedNotice: "\n\n_(interrupted)_" },
  attribution: { icon: ":robot_face:", name: "Paimon", role: "Assistant", review: "unconfirmed" },  // off by default
  recovery: { maxPagesPerChannel: 10, watchedThreadsLimit: 100, resyncBufferLimit: 10_000 },
  stateStore: new FileStateStore({ dir: "/var/lib/my-agent/gabriel" }),
  logger: consoleLogger("info"),        // any pino-style { debug, info, warn, error }(obj, msg)
  metrics: { increment(name, labels) {}, observe(name, value, labels) {} },
  advanced: { fetch, createWebSocket, metadata: { userAgent }, endpoints, gatewayServer, startTimeoutMs: 60_000 },
});
```

`advanced.fetch` and `advanced.createWebSocket` let you route traffic through a proxy or plug
in test doubles (see [`test/harness.ts`](https://github.com/Easy-Ez/gabriel-slack/blob/main/sdks/node/test/harness.ts)).

## CLI

```bash
npx gabriel auth login your-workspace           # sign in in a dedicated Chrome window; saved with mode 0600
npx gabriel auth extract --port 9222            # read from a Chrome you started with --remote-debugging-port and signed in
pbpaste | npx gabriel auth parse-curl --save   # "Copy as cURL" of any request from your signed-in Slack tab
npx gabriel auth status                        # verify and list workspaces (secrets redacted)
npx gabriel auth logout --yes
```

`auth login` uses [`signInWithBrowser()`](#signing-in-from-an-app); set `GABRIEL_CHROME_PATH` when
Chrome / Chromium is not installed in a standard location.

## Implementation status

| Area | Status |
| :--- | :--- |
| Core: auth exchange / refresh / AUTH_DEAD, HTTP + EdgeAPI, WebSocket state machine | ✅ |
| Events, hydration, ordering, executor, waitForReply, middleware, pull stream | ✅ |
| Chat (chunking, FIFO, idempotency, unread guard), streams, threads, files, search, reactions, users, channels | ✅ |
| Presence + profile/DND pools, reconnect replay, snapshot diffing | ✅ |
| Loop guard, `scope()` sandbox, `toLLMContext()` | ✅ |
| Gap recovery (cold + fast-reconnect reconcile), `MemoryStateStore`, `FileStateStore` | ✅ |
| `RedisStateStore`, multi-replica leases (`guard.lease_expired`) | ⏳ planned |
| `gabriel-daemon` / `DaemonTransport` | ⏳ planned |
| `gabriel auth login` / `signInWithBrowser()` (dedicated Chrome window, system Chrome via CDP), `listWorkspaces()` | ✅ |
| `gabriel auth extract` (read from a Chrome started with a debug port) | ✅ |
| OpenTelemetry spans (metrics hook available today) | ⏳ planned |

## License

MIT © 2026 EzEasy
