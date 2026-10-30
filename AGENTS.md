# AGENTS.md — Guide for coding agents

Gabriel is a typed, AI-friendly real-time SDK family for the Slack Web client protocol.
This file tells coding agents (and humans) how the repository is organized and which rules
every change must follow. Human-oriented details live in [CONTRIBUTING.md](./CONTRIBUTING.md).

## Layout

```text
spec/                  Language-neutral contract (normative): behavior.md, event JSON Schema, conformance fixtures
sdks/node/             TypeScript reference SDK (npm: gabriel-slack) + the `gabriel` CLI
sdks/go/, sdks/python/ Planned SDKs (README describes the intended API shape)
docs/                  Architecture overview (EN) and user guide (zh-CN)
scripts/               spec-check and leak check
```

## Rules

1. **Spec first.** A behavior change updates `spec/behavior.md`, `spec/schema/events.schema.json`
   and `spec/fixtures/` together with the implementation. `pnpm spec:check` keeps the TypeScript
   event map and the schema in sync.
2. **Placeholders only.** Never commit tokens, cookies, reconnect tickets, emails, real message
   text, or real workspace / user / channel IDs. Use `X0123456789` or `X0NAME0001` style IDs
   (`U0ALICE0001`, `C0GENERAL01`) and `your-workspace` as the domain.
3. **Redact credentials** everywhere (logs, errors, examples): keep at most a prefix and the length,
   e.g. `xoxc-12345...[REDACTED, len=109]` — use `redactSecret()`.
4. **Tests never touch a real workspace.** `sdks/node/test/harness.ts` is an in-memory Slack
   (Web API, EdgeAPI, cookie exchange, WebSocket gateway). Add a test with every change.
5. **Live checks are opt-in and read-only by default.** The `examples/smoke-*.ts` and
   `examples/live-roundtrip.ts` scripts write only to a channel the account owner explicitly chose,
   and clean up after themselves.
6. **Public docs are curated.** `docs/` only contains the files listed in `scripts/oss-leak-check.mjs`
   (`PUBLIC_DOCS`). Write for users and contributors; do not add research logs, review minutes,
   verification matrices or decision records.
7. **Keep safe defaults.** Loop protection on, echoes of the client's own writes filtered, sandbox
   available, bounded queues, credentials only sent to https Slack hosts.

## Commands

```bash
pnpm install
pnpm test          # unit, fake-Slack integration and conformance tests
pnpm typecheck
pnpm build
pnpm spec:check    # TypeScript events ⇔ JSON Schema ⇔ fixtures
pnpm oss:check     # no secrets, real IDs, internal markers or unlisted docs in tracked files
```

Run all of them before committing; CI runs the same set on Node 20, 22 and 24.
