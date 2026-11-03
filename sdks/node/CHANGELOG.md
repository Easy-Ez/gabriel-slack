# Changelog

All notable changes to `gabriel-slack` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may break).

## [Unreleased]

## [0.2.0] - 2026-11-03

### Added
- `signInWithBrowser()`: signs in through a dedicated Chrome / Chromium window with a temporary
  profile and returns the session cookie plus the signed-in workspaces, for apps that store the
  cookie themselves (e.g. in the OS keychain). Prints nothing, reports progress through `onStatus`,
  supports `signal` / `timeoutMs` / `browserPath`, and always deletes the temporary profile.
- `listWorkspaces()`: verifies a session cookie and lists its workspaces without exposing tokens.
- Error codes `BROWSER_NOT_FOUND` and `SIGN_IN_TIMEOUT`.

### Changed
- `gabriel auth login` is built on `signInWithBrowser()`. It now finds the DevTools port through
  Chrome's `DevToolsActivePort` file instead of guessing a free port, notices a closed sign-in window
  on macOS, and fails with `BROWSER_NOT_FOUND`, `SIGN_IN_TIMEOUT` or `AUTH_DEAD` instead of
  `INVALID_ARGUMENT` / `INVALID_STATE` / `NETWORK_TIMEOUT`.

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
