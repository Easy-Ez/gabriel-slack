# Contributing to Gabriel

Thanks for helping! This repository hosts several SDKs that share one contract.

## Layout

| Path | Purpose |
| :--- | :--- |
| `spec/` | Language-neutral contract: `behavior.md`, event JSON Schema, conformance fixtures. **Change the spec first**, then the SDKs. |
| `sdks/node/` | TypeScript reference SDK (`gabriel-slack`) and the `gabriel` CLI. |
| `sdks/python/` | Python SDK (distribution `gabriel-slack`, import `gabriel`), asyncio, managed with `uv`. |
| `sdks/go/` | Future SDK. |
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

Python SDK (`uv` required):

```bash
cd sdks/python
uv sync
uv run pytest              # unit + fake-Slack integration + conformance fixtures + schema validation
uv run mypy                # strict
uv run ruff check src tests examples && uv run ruff format --check src tests examples
```

Tests never touch a real workspace: `sdks/node/test/harness.ts` and `sdks/python/tests/harness.py`
provide an in-memory Slack (Web API, EdgeAPI, cookie exchange and WebSocket gateway). Please add a
test with every change, in every SDK the change affects.

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
   idioms (see the planned shape in `sdks/go/README.md` and the Python SDK in `sdks/python`).
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

## Releasing

Each SDK is released from its own tag:

| SDK | Tag | Workflow |
| :--- | :--- | :--- |
| Node.js (`gabriel-slack` on npm) | `node-v<version>` | `.github/workflows/release-node.yml` |
| Go (planned) | `sdks/go/v<version>` (Go's rule for modules in subdirectories) | — |
| Python (`gabriel-slack` on PyPI) | `python-v<version>` | `.github/workflows/release-python.yml` |

Node.js release steps:

1. Bump `version` in `sdks/node/package.json` and add a `## [<version>]` section to `sdks/node/CHANGELOG.md`.
2. Optional rehearsal: run the **release-node** workflow manually (always a dry run).
3. Tag and push: `git tag node-v<version> && git push origin node-v<version>`.
4. The workflow verifies the tag matches the package version, runs every check, publishes to npm with
   provenance and creates the GitHub release from the changelog section.

Packages are only published from GitHub Actions. `publishConfig.provenance` makes a local `npm publish`
fail on purpose, so a developer machine's npm credentials can never publish the package.

**Authentication:** npm trusted publishing (OIDC) bound to this repository, the `release-node.yml`
workflow and the `npm` environment. No npm token is stored anywhere, and the environment is only
reachable from `node-v*` tag runs.

**After publishing:** npm runs an automated review on every new version. Until it finishes, the version
shows as "Validating" on npmjs.com, cannot be installed yet, and `latest` keeps pointing at the previous
version. This normally clears on its own; nothing needs to be re-run.

Python release steps:

1. Bump `version` in `sdks/python/pyproject.toml` (`uv version <version>` from `sdks/python`) and add a
   `## [<version>]` section to `sdks/python/CHANGELOG.md`.
2. Optional rehearsal: run the **release-python** workflow manually (builds and checks, never uploads).
3. Tag and push: `git tag python-v<version> && git push origin python-v<version>`.
4. The workflow verifies the tag matches the package version, runs every check, publishes to PyPI with
   attestations and creates the GitHub release from the changelog section.

**Authentication:** PyPI trusted publishing (OIDC) bound to this repository, the `release-python.yml`
workflow and the `pypi` environment. No PyPI token is stored anywhere.

## Reporting security issues

Please do not open public issues for vulnerabilities. See [SECURITY.md](./SECURITY.md).
