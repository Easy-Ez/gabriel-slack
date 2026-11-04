# Changelog

All notable changes to the Python SDK are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
