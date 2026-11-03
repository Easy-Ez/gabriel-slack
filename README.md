# Gabriel

**Typed, AI-friendly real-time SDKs for the Slack Web client protocol.**

Gabriel connects with the same session the Slack web app uses (the `d` session cookie,
from which a workspace token is derived). That gives personal and internal automation a
real-time event stream (messages, threads, reactions, typing, presence, status, Do Not
Disturb, unread badges) and the actions to respond to it, without installing an OAuth app
or exposing a webhook. It is built for wiring Slack activity into LLM / AI-agent workflows
with safe defaults.

[中文说明](./README.zh-CN.md)

> [!IMPORTANT]
> Gabriel is a personal, non-commercial learning project by [EzEasy](https://github.com/Easy-Ez). It is an **independent, unofficial** project and is not affiliated with or endorsed
> by Slack. It acts as the signed-in person. Use it only with accounts and workspaces you
> are authorized to automate, follow your organization's policies and Slack's terms, and
> prefer an official Slack app (for example [Bolt](https://slack.dev/bolt-js)) whenever one
> is available to you. Treat the session cookie like a password.

## SDKs

| Language | Package | Status |
| :--- | :--- | :--- |
| TypeScript / Node.js | [`gabriel-slack`](./sdks/node) | **Preview (0.2)**: reference implementation |
| Go | [`sdks/go`](./sdks/go) | Planned |
| Python | [`gabriel-slack`](./sdks/python) (import `gabriel`) | **Preview (0.1)**: asyncio, full parity with the reference except the in-app sign-in API (`signInWithBrowser()`) |

Every SDK implements the same language-neutral contract in [`spec/`](./spec): a behavior
specification, a JSON Schema for every event, and shared conformance fixtures.

## Quick start

### Node.js (≥ 20.10)

```bash
npm install gabriel-slack
```

```typescript
import { GabrielClient } from "gabriel-slack";

const client = GabrielClient.fromCookie({
  cookieD: process.env.GABRIEL_COOKIE_D!, // from a secret store, never hard-coded
  workspace: "your-workspace",
});

client.onMessage({ mentionsMe: true }, async (evt, ctx) => {
  const stream = await ctx.replyStream({ initialMarkdown: "_thinking…_" });
  for await (const chunk of yourLlm.stream(evt.toLLMContext(), { signal: stream.signal })) {
    stream.append(chunk);
  }
  await stream.finish();
});

await client.start();
```

### Python (≥ 3.11)

```bash
pip install gabriel-slack      # or: uv add gabriel-slack
```

```python
import asyncio
import os

from gabriel import GabrielClient


async def main() -> None:
    client = GabrielClient.from_cookie(
        cookie_d=os.environ["GABRIEL_COOKIE_D"],  # from a secret store, never hard-coded
        workspace="your-workspace",
    )

    @client.on_message(mentions_me=True)
    async def answer(evt, ctx):
        stream = await ctx.reply_stream(initial_markdown="_thinking…_")
        async for chunk in your_llm.stream(evt.to_llm_context(), signal=stream.signal):
            stream.append(chunk)
        await stream.finish()

    async with client:
        await asyncio.Event().wait()


asyncio.run(main())
```

With those few lines you already get: token derivation and refresh, reconnects and
gap backfill, ordering within each channel, suppression of echoes from your own messages,
an auto-reply circuit breaker, automatic splitting of long messages, and cancellation when
the triggering message is deleted.

Full API documentation:

* [Node.js SDK guide](./sdks/node/README.md): events, handler context, actions, `scope()`, reliability, CLI.
* [Python SDK guide](./sdks/python/README.md): the same surface in Python (snake_case, `async with`,
  decorator handlers, durations in seconds).
* [Behavior specification](./spec/behavior.md) and [architecture overview](./docs/architecture.md).

## Highlights

* **One primary event per frame**: `message.created` covers channel messages, thread replies,
  broadcasts and bot posts, so one message never fires overlapping handlers.
* **Ordered and hydrated**: per-channel arrival order is preserved while names, mentions and
  Markdown are resolved in parallel within a 300 ms budget.
* **Loop-safe**: every event carries an `origin` (`this_client`, `self_other_client`,
  `other_user`, `bot`); echoes of this client's writes are dropped by default.
* **AI-ready**: `toLLMContext()` / `to_llm_context()` wraps message content in an untrusted-content envelope;
  `client.scope()` gives LLM tools a least-privilege view (channel allow-lists, no search,
  reply only in the triggering thread, approval hooks).
* **Streaming replies** that work with session tokens: a single request in flight, the latest
  content wins, code fences are closed while streaming, and long output continues in a new message.
* **Presence at scale**: reference-counted watches, enforced gateway frame limits, and no
  repeated snapshots after reconnects.
* **Escape hatch**: `client.core` exposes raw frames and any Web API / EdgeAPI method.

## Repository layout

```text
spec/            Language-neutral contract: behavior.md, events JSON Schema, conformance fixtures
sdks/node/       TypeScript reference SDK (gabriel-slack) + `gabriel` CLI
sdks/go/         Go SDK (planned)
sdks/python/     Python SDK (distribution gabriel-slack, import `gabriel`) + `gabriel` CLI
docs/            Architecture overview and a Chinese user guide
scripts/         Spec consistency check and leak check
```

## Development

```bash
pnpm install
pnpm test          # unit, integration and conformance tests
pnpm typecheck
pnpm spec:check    # TypeScript events ⇔ JSON Schema ⇔ fixtures
pnpm oss:check     # no credentials, real IDs, emails or local paths in any tracked file
```

Python SDK (from `sdks/python`, uses [uv](https://docs.astral.sh/uv/)):

```bash
uv sync
uv run pytest
uv run mypy
uv run ruff check src tests examples
```

See [CONTRIBUTING.md](./CONTRIBUTING.md) and the [Code of Conduct](./CODE_OF_CONDUCT.md) for conventions, how to add a language SDK, and the
fixture rules. Report vulnerabilities as described in [SECURITY.md](./SECURITY.md).

## License

[MIT](./LICENSE) © 2026 EzEasy
