# Changelog

All notable changes to the Python SDK are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed
- **Behavior change: `markdown` no longer contains link previews.** Slack's unfurls (attachments
  with `original_url`, `from_url`, `is_app_unfurl` or `is_msg_unfurl`, e.g. a GitHub pull request
  or a shared Slack message) used to be appended to `MessageCreatedEvent.markdown`,
  `MessageUpdatedEvent.current_markdown` / `previous_markdown`, `AiStream*Event.markdown` and the
  `markdown` of thread, history, activity and search messages, so a message that was only a link
  could turn into thousands of characters the sender never wrote. They are now left out and given
  separately (see below). Legacy attachments without these markers (e.g. posted by bots) are still
  rendered into `markdown`. Code that searched `markdown` for preview text should read the new
  fields instead.
- `to_llm_context()` lists link previews after the body in a `<link_previews>` section, inside the
  untrusted envelope and labelled as generated automatically, not written by the sender. Thread
  transcripts (`formatted_transcript`) show one `[link preview, auto-generated: …]` line per
  preview.

### Added
- `LinkPreview` (`url`, `service_name`, `title`, `text`; serialized as `serviceName`) and
  `MessageCreatedEvent.link_previews`, `MessageUpdatedEvent.current_link_previews`, and
  `link_previews` on `ThreadMessage`, `HistoryMessage` (also activity entries' messages) and
  `SearchMessageHit`; empty lists when there is none. A `message_changed` that only adds a preview
  still emits `message.updated`, with the preview in `current_link_previews`. Names mentioned inside
  attachments are now looked up for rendering.

## [0.6.0] - 2026-11-12

### Added
- `user.profile_changed` (`UserProfileChangedEvent`): a user's display name, real name or handle
  changed (`changed`, `previous`). The user cache is updated first, so `users.resolve()` returns the
  new values. Status- or avatar-only changes do not trigger it.
- `presence.watch_profiles(user_ids)`: subscribe to profile changes only (no presence, no DND).
  Slack pushes profile changes only for subscribed users; up to 900 per connection.

### Changed
- `presence.watch(…, track_profile_and_dnd=True)` and `watch_profiles` share the 900-user profile
  pool; DND subscriptions are only sent for `track_profile_and_dnd` watchers.
- `user.status_changed` keeps working after the user cache entry expires (the last record of each
  subscribed user is kept while it is subscribed).

## [0.5.0] - 2026-11-12

### Added
- `HydrationOptions.backfill_budget` (default 5.0 s): how long a gap recovery waits for the names
  of all its messages before dispatching them. `0` skips the shared lookup.

### Changed
- `HydrationOptions.hydrate_budget` defaults to 1.0 s (was 0.3). One name lookup round trip
  commonly takes 250–450 ms, so the old budget ran out on most cache misses.

### Fixed
- Backfilled events no longer arrive without sender, channel and mention names after a restart:
  gap recovery resolves the names of the whole batch before dispatching it.
- An event whose name lookup outlasted its budget is rendered again with the names known by then
  when it leaves its lane and when each handler starts, instead of keeping raw IDs.

## [0.4.1] - 2026-11-10

### Fixed
- `client.activity.list` / `iterate` and `client.channels.get_messages` no longer fail with
  `too_many_channels` when one page points at more than 20 conversations: messages are fetched
  in calls of at most 20 channels.

## [0.4.0] - 2026-11-10

### Added
- `client.channels.list_direct()` / `iterate_direct(active_since=…)`: your 1:1 and group direct
  conversations with the timestamp of their newest message, including closed ones.
- `client.activity.list(types)` / `iterate(types, since=…)`: the Activity feed (mentions, DMs,
  thread replies, reactions). Each entry is resolved to its full message; use
  `types=["at_user"]` for messages that name you.
- `client.channels.get_messages(channel, timestamps)`: fetches specific messages, thread replies
  included.
- `HistoryMessage` has `mentions`, `mention_reasons`, `subtype` and `is_deleted`; `ThreadMessage`
  has `mentions` and `mention_reasons`; `SearchMessageHit` has `mentions`, `mention_reasons`,
  `files`, `attribution` and `channel.is_im` / `channel.is_mpim`.
- Scoped views expose the new reads, limited by `read.channels`; `channels.get_history` accepts
  `inclusive` there too.
- Types `ActivityItem`, `ActivityPage`, `DirectConversation`, `DirectPage`.

None of the new reads marks anything read.

## [0.3.0] - 2026-11-04

### Added

- `MessageCreatedEvent.mention_reasons`: why the message mentions you, as `"direct"` (you, by ID),
  `"usergroup"` (a user group you belong to) and / or `"broadcast"` (`@here` / `@channel` /
  `@everyone`), each at most once in that order; empty when it does not. `is_mentioning_me` is
  unchanged and equals `len(mention_reasons) > 0`. To react only when you are named, use
  `"direct" in evt.mention_reasons`.
- `Mention.includes_me` for user group mentions: `True` / `False` when the SDK knows whether you
  belong to the group (user group preload or membership events), `None` otherwise.
- `MentionReason` type.

## [0.2.0] - 2026-11-03

### Added

- `sign_in_with_browser()`: signs in through a dedicated Chrome / Chromium window with a temporary
  profile and returns `SignInResult(cookie_d, workspaces)`, for apps that store the cookie
  themselves (e.g. in the OS keychain). Prints nothing, reports progress through `on_status`,
  supports `signal` / `timeout` / `browser_path` and task cancellation, and always deletes the
  temporary profile. Port of the Node SDK's `signInWithBrowser()`.
- `list_workspaces()`: verifies a session cookie and lists its workspaces (`WorkspaceInfo`) without
  exposing tokens.
- Error codes `BROWSER_NOT_FOUND` and `SIGN_IN_TIMEOUT`.
- Opt-in agent attribution line on written messages, e.g.
  `:robot_face: Paimon (Assistant) · confirmed by @Me`, so readers can tell that an agent wrote a
  message sent with a session token. Set a client default with `GabrielClient(attribution=...)`.
  The default is either a preset (`AttributionPreset`: `icon`, `name`, `role`, `review`) or a
  custom mrkdwn line (`AttributionCustom`: `text`, which takes a `{self}` placeholder or a
  function of `AttributionInfo`). It is off by default, and existing calls behave exactly as
  before.
- `attribution=` override per write on `chat.send()`, `chat.update()`, `chat.stream()`,
  `chat.create_stream()`, `EventContext.reply()` and `EventContext.reply_stream()`. Pass an
  `AttributionPatch` merged over the default key by key, a custom line, or `False` to drop the line.
  `review` is `"unconfirmed"` (default), `"confirmed"` or `"hidden"`. `"confirmed"` is accepted only
  per write, never as the client default.
- Every chunk, `chat.update` and stream placeholder / refresh / continuation carries the line, and
  chunk budgets account for it. The line is a context block tagged with `ATTRIBUTION_BLOCK_ID`; the
  Markdown body then goes in a `markdown` block with a plain-text `text` fallback.
- `chat.preview_attribution()` and the pure `render_attribution()` return the exact line
  (`RenderedAttribution(text, block)`) without sending anything. `DEFAULT_ATTRIBUTION_ICON` is
  exported.
- Received messages expose the line as `attribution: MessageAttribution(text, review)` on
  `MessageCreatedEvent`, `MessageUpdatedEvent`, thread messages and `HistoryMessage`, with the line
  removed from `markdown`. `parse_attribution()` extracts it from raw blocks. `to_llm_context()` and
  thread transcripts mark it as self-declared and unverified.

### Changed

- `gabriel auth login` is built on `sign_in_with_browser()`. It now finds the DevTools port through
  Chrome's `DevToolsActivePort` file instead of guessing a free port, notices a closed sign-in window
  on macOS, and fails with `BROWSER_NOT_FOUND`, `SIGN_IN_TIMEOUT` or `AUTH_DEAD` instead of
  `INVALID_ARGUMENT` / `INVALID_STATE` / `NETWORK_TIMEOUT`.

### Security

- Attribution lines reject broadcast and user-group mentions and escape preset fields. Scoped views
  (`scope()`) reject per-write `attribution` overrides, so an LLM tool cannot claim `"confirmed"`
  or remove the line.

## [0.1.0] - 2026-11-03

### Added

- Initial Python / asyncio implementation of the Gabriel contract, at parity with the TypeScript
  reference SDK: `GabrielCore` (cookie exchange, token refresh / `AUTH_DEAD`, Web API + EdgeAPI,
  WebSocket state machine) and `GabrielClient` (typed events, hydration, ordering, executor,
  `wait_for_reply`, chat with chunking and streams, threads, files, search, presence, gap
  recovery, `scope()` sandbox, `to_llm_context()`), `MemoryStateStore` / `FileStateStore` and the
  `gabriel auth` CLI.
- Every conformance fixture in `spec/fixtures/normalize` passes; every event validates against
  `spec/schema/events.schema.json`.
