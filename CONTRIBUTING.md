# Contributing to Gabriel

Thanks for helping! This repository hosts several SDKs that share one contract.

## Layout

| Path | Purpose |
| :--- | :--- |
| `spec/` | Language-neutral contract: `behavior.md`, event JSON Schema, conformance fixtures. **Change the spec first**, then the SDKs. |
| `sdks/node/` | TypeScript reference SDK (`gabriel-slack`) and the `gabriel` CLI. |
| `sdks/go/`, `sdks/python/` | Future SDKs. |
| `docs/` | Curated public docs: architecture overview (EN) and user guide (zh-CN). |
| `scripts/` | `spec-check`, open-source export and leak check. |

## Development setup

```bash
corepack enable            # pnpm 9
pnpm install
pnpm test                  # vitest: unit + fake-Slack integration + conformance fixtures
pnpm typecheck
pnpm spec:check
pnpm oss:check
```

Tests never touch a real workspace: `sdks/node/test/harness.ts` provides an in-memory Slack
(Web API, EdgeAPI, cookie exchange and WebSocket gateway). Please add a test with every change.

## Ground rules

1. **No secrets, no real identifiers.** Never commit tokens, cookies, reconnect tickets, emails,
   real message text or real workspace / user / channel IDs, including in tests, fixtures, docs
   and examples. Use placeholder IDs: `X0123456789` or `X0NAME0001` (`U0ALICE0001`,
   `C0GENERAL01`). `pnpm oss:check` enforces this.
2. **Credentials stay redacted.** Log at most a prefix and the length (`redactSecret`).
3. **Spec first.** Behavior changes update `spec/behavior.md`, the schema and fixtures in the same
   pull request; `pnpm spec:check` keeps the TypeScript event map and the schema in sync.
4. **Least surprise for automation.** Defaults must stay safe: loop protection on, echoes of your
   own messages filtered, sandbox available, no unbounded queues.
5. **Validate inputs** at public API boundaries (IDs, timestamps, emoji names, file names) and
   never send credentials to non-Slack hosts.
6. **Curated docs.** `docs/` only holds files listed in `PUBLIC_DOCS` (`scripts/oss-leak-check.mjs`).
   Write for users and contributors; research logs, review minutes and verification records do not
   belong in this repository.
7. Pull request titles are short imperative sentences; describe *why* in the body.

## Adding a language SDK

1. Create `sdks/<language>/` with its own README, build and test setup, using the language's
   idioms (see the planned shapes in `sdks/go/README.md` and `sdks/python/README.md`).
2. Implement the low-level core first (credentials, HTTP, WebSocket state machine), then the
   high-level client, following `spec/behavior.md`.
3. Load every file in `spec/fixtures/normalize/` in your test suite and assert the subset match
   described in `spec/README.md`.
4. Serialize events with the field names of `spec/schema/events.schema.json` (snake_case aliases
   are fine in Python as long as JSON output matches the schema).
5. Add the SDK to the table in the root README and to CI.

## Fixtures

New protocol behavior should come with a fixture in `spec/fixtures/normalize/`: a minimal raw
frame, the context it needs, and the expected event subset. Keep one behavior per file.

## Reporting security issues

Please do not open public issues for vulnerabilities. See [SECURITY.md](./SECURITY.md).
