# Changelog

All notable changes to `gabriel-slack` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may break).

## [Unreleased]

## [0.7.0] - 2026-11-13

### Changed
- **Behavior change: `markdown` no longer contains link previews.** Slack's unfurls (attachments
  with `original_url`, `from_url`, `is_app_unfurl` or `is_msg_unfurl`, e.g. a GitHub pull request
  or a shared Slack message) used to be appended to `message.created.markdown`,
  `message.updated.currentMarkdown` / `previousMarkdown`, `ai.stream_*.markdown` and the
  `markdown` of thread, history, activity and search messages, so a message that was only a link
  could turn into thousands of characters the sender never wrote. They are now left out and given
  separately (see below). Legacy attachments without these markers (e.g. posted by bots) are still
  rendered into `markdown`. Code that searched `markdown` for preview text should read the new
  fields instead.
- `toLLMContext()` lists link previews after the body in a `<link_previews>` section, inside the
  untrusted envelope and labelled as generated automatically, not written by the sender. Thread
  transcripts (`formattedTranscript`) show one `[link preview, auto-generated: …]` line per preview.

### Added
- `LinkPreview` (`{ url, serviceName?, title?, text? }`) and `message.created.linkPreviews`,
  `message.updated.currentLinkPreviews`, and `linkPreviews` on `ThreadMessage`, `HistoryMessage`
  (also activity entries' messages) and `SearchMessageHit`; empty arrays when there is none. A
  `message_changed` that only adds a preview still emits `message.updated`, with the preview in
  `currentLinkPreviews`. Names mentioned inside attachments are now looked up for rendering.

## [0.6.0] - 2026-11-12

### Added
- `user.profile_changed`: a user's display name, real name or handle changed (`changed`,
  `previous`). The user cache is updated first, so `users.resolve()` returns the new values.
  Status- or avatar-only changes do not trigger it.
- `presence.watchProfiles(userIds)`: subscribe to profile changes only (no presence, no DND).
  Slack pushes profile changes only for subscribed users; up to 900 per connection.

### Changed
- `presence.watch(…, { trackProfileAndDnd: true })` and `watchProfiles` share the 900-user profile
  pool; DND subscriptions are only sent for `trackProfileAndDnd` watchers.
- `user.status_changed` keeps working after the user cache entry expires (the last record of each
  subscribed user is kept while it is subscribed).

## [0.5.0] - 2026-11-12

### Added
- `hydration.backfillBudgetMs` (default 5000): how long a gap recovery waits for the names of all
  its messages before dispatching them. `0` skips the shared lookup.

### Changed
- `hydration.hydrateBudgetMs` defaults to 1000 (was 300). One name lookup round trip commonly takes
  250–450 ms, so the old budget ran out on most cache misses.

### Fixed
- Backfilled events no longer arrive without sender, channel and mention names after a restart:
  gap recovery resolves the names of the whole batch before dispatching it.
- An event whose name lookup outlasted its budget is rendered again with the names known by then
  when it leaves its lane and when each handler starts, instead of keeping raw IDs.

## [0.4.1] - 2026-11-10

### Fixed
- `client.activity.list` / `iterate` and `client.channels.getMessages` no longer fail with
  `too_many_channels` when one page points at more than 20 conversations: messages are fetched
  in calls of at most 20 channels.

## [0.4.0] - 2026-11-10

### Added
- `client.channels.listDirect()` / `iterateDirect({ activeSince })`: your 1:1 and group direct
  conversations with the timestamp of their newest message, including closed ones.
- `client.activity.list({ types })` / `iterate({ types, since })`: the Activity feed (mentions,
  DMs, thread replies, reactions). Each entry is resolved to its full message; use
  `types: ["at_user"]` for messages that name you.
- `client.channels.getMessages({ channel, timestamps })`: fetches specific messages, thread
  replies included.
- `HistoryMessage` has `mentions`, `mentionReasons`, `subtype` and `isDeleted`; `ThreadMessage`
  has `mentions` and `mentionReasons`; `SearchMessageHit` has `mentions`, `mentionReasons`,
  `files`, `attribution` and `channel.isIm` / `channel.isMpim`.
- Scoped views expose the new reads, limited by `read.channels`; `channels.getHistory` accepts
  `inclusive` there too.
- Types `ActivityItem`, `ActivityType`, `DirectConversation`.

None of the new reads marks anything read.

## [0.3.0] - 2026-11-04

### Added
- `message.created` has `mentionReasons`: why the message mentions you, as `"direct"` (you, by ID),
  `"usergroup"` (a user group you belong to) and / or `"broadcast"` (`@here` / `@channel` /
  `@everyone`), each at most once in that order; empty when it does not. `isMentioningMe` is
  unchanged and equals `mentionReasons.length > 0`. To react only when you are named, use
  `mentionReasons.includes("direct")`.
- User group entries in `mentions` have `includesMe: true | false` when the SDK knows whether you
  belong to the group (user group preload or membership events), and leave it out otherwise.
- `MentionReason` type.

## [0.2.0] - 2026-11-03

### Added
- `signInWithBrowser()`: signs in through a dedicated Chrome / Chromium window with a temporary
  profile and returns the session cookie plus the signed-in workspaces, for apps that store the
  cookie themselves (e.g. in the OS keychain). Prints nothing, reports progress through `onStatus`,
  supports `signal` / `timeoutMs` / `browserPath`, and always deletes the temporary profile.
- `listWorkspaces()`: verifies a session cookie and lists its workspaces without exposing tokens.
- Error codes `BROWSER_NOT_FOUND` and `SIGN_IN_TIMEOUT`.
- Opt-in agent attribution line, a tagged context block above the message body such as
  `:robot_face: Paimon (Assistant) · confirmed by @Me`. It shows readers that an agent wrote a message
  that would otherwise look like the account owner's own. Off by default; existing calls behave
  exactly as before.
- `GabrielClientOptions.attribution` sets the client default, either as preset fields
  (`{ icon, name, role, review }`) or as custom mrkdwn `{ text }`, which can be a string with a
  `{self}` placeholder or a function. `SendOptions`, `ReplyOptions`, `CreateStreamOptions` and
  `replyStream()` take a per-write `attribution` override; pass `false` to leave the line off.
- `review` can be `"unconfirmed"` (the default), `"confirmed"` or `"hidden"`. `"confirmed"` is
  accepted only per write, never as the client default.
- The line is carried on every chunk, every `chat.update` edit and every stream placeholder, refresh
  and continuation, and chunk budgets leave room for it.
- `chat.previewAttribution()` and the pure `renderAttribution()` return the exact line without
  sending it. `parseAttribution()`, `ATTRIBUTION_BLOCK_ID`, `DEFAULT_ATTRIBUTION_ICON` and the
  `Attribution*` / `RenderedAttribution` / `MessageAttribution` types are exported too.
- Received messages expose a self-declared line as `attribution: { text, review? }` on
  `message.created`, `message.updated`, thread messages and `HistoryMessage`, and remove it from
  `markdown`. `toLLMContext()` and thread transcripts label it as self-declared and unverified.

### Changed
- `gabriel auth login` is built on `signInWithBrowser()`. It now finds the DevTools port through
  Chrome's `DevToolsActivePort` file instead of guessing a free port, notices a closed sign-in window
  on macOS, and fails with `BROWSER_NOT_FOUND`, `SIGN_IN_TIMEOUT` or `AUTH_DEAD` instead of
  `INVALID_ARGUMENT` / `INVALID_STATE` / `NETWORK_TIMEOUT`.

### Security
- Attribution text that contains broadcast mentions (`@here`, `@channel`, `@everyone`) or user-group
  mentions is rejected, and preset fields are escaped. Scoped views reject per-write attribution
  overrides, so an LLM tool can't claim `"confirmed"` or drop the line.

## [0.1.1] - 2026-10-30

### Changed
- First release published from GitHub Actions through npm trusted publishing (OIDC) with provenance;
  no long-lived npm token is involved. The package contents are unchanged from 0.1.0.

## [0.1.0] - 2026-10-30

### Added
- `GabrielCore`: cookie → token exchange with singleflight refresh and terminal `AUTH_DEAD`;
  Web API / EdgeAPI transport with timeouts, per-method 429 cool-downs and idempotency-aware
  retries; WebSocket state machine (epochs, 10 s ping, 25 s dead-peer, 285 s fast-reconnect
  tickets, `goodbye`, full-jitter backoff); raw frame subscriptions and protocol-drift reports.
- `GabrielClient`: typed events (one primary event per frame), hydration with a time budget,
  per-channel ordered dispatch, bounded keyed executor, handler timeouts, middleware,
  `waitForReply`, pull-mode `events()`.
- Chat: Markdown chunking with fence healing, per-channel FIFO, priority send semaphore,
  `client_msg_id` idempotency, `preserveUnreadCursor`, latest-wins streaming controller.
- Threads, files (3-step upload, guarded downloads), search, reactions, users, channels APIs.
- Presence and profile/DND subscription pools with gateway limits and reconnect replay.
- Origin detection, loop breaker, `scope()` sandbox, `toLLMContext()`.
- Gap recovery with watermarks; `MemoryStateStore`, `FileStateStore`.
- `gabriel` CLI: `auth login` (dedicated Chrome sign-in window), `auth extract` (read from a Chrome debug port), `auth parse-curl`, `auth status`, `auth logout`.
- `examples/smoke-readonly.ts` (zero-write) and `examples/smoke-write.ts` (writes to one channel, then cleans up) live smoke tests.

### Fixed
- File share messages created by `files.upload` are now recognized as this client's own echo.
