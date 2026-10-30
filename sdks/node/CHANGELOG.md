# Changelog

All notable changes to `gabriel-slack` are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/) (pre-1.0: minor versions may break).

## [Unreleased]

## [0.1.0] - Unreleased

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
