# Changelog

All notable changes to the Python SDK are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
