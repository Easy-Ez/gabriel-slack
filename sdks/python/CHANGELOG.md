# Changelog

All notable changes to the Python SDK are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- `sign_in_with_browser()`: signs in through a dedicated Chrome / Chromium window with a temporary
  profile and returns `SignInResult(cookie_d, workspaces)`, for apps that store the cookie
  themselves (e.g. in the OS keychain). Prints nothing, reports progress through `on_status`,
  supports `signal` / `timeout` / `browser_path` and task cancellation, and always deletes the
  temporary profile. Port of the Node SDK's `signInWithBrowser()`.
- `list_workspaces()`: verifies a session cookie and lists its workspaces (`WorkspaceInfo`) without
  exposing tokens.
- Error codes `BROWSER_NOT_FOUND` and `SIGN_IN_TIMEOUT`.

### Changed

- `gabriel auth login` is built on `sign_in_with_browser()`. It now finds the DevTools port through
  Chrome's `DevToolsActivePort` file instead of guessing a free port, notices a closed sign-in window
  on macOS, and fails with `BROWSER_NOT_FOUND`, `SIGN_IN_TIMEOUT` or `AUTH_DEAD` instead of
  `INVALID_ARGUMENT` / `INVALID_STATE` / `NETWORK_TIMEOUT`.

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
