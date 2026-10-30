# Gabriel for Python (planned)

The Python SDK (distribution `gabriel-slack`, import package `gabriel`, asyncio) will implement the same contract as the TypeScript
reference SDK:

* Behavior: [`spec/behavior.md`](../../spec/behavior.md)
* Event payloads: [`spec/schema/events.schema.json`](../../spec/schema/events.schema.json)
* Conformance: every case in [`spec/fixtures/normalize`](../../spec/fixtures/normalize) must pass.

## Planned API shape

| Concept | Python idiom |
| :--- | :--- |
| Lifecycle | `async with GabrielClient.from_cookie(cookie_d=..., workspace=...) as client:` |
| Push events | `@client.on(MessageCreated)` + `async def handle(evt, ctx): ...` |
| Pull events | `async for evt in client.events():` |
| Cancellation | `asyncio` task cancellation |
| Options | keyword arguments: `limit=50, thread_ts=ts` |
| Streaming reply | `async with client.chat.stream(channel=..., thread_ts=...) as s: s.append(chunk)` |
| Naming | snake_case fields (`is_mentioning_me`, `thread_root_ts`) mapping 1:1 to the schema |

Interested in contributing? See [CONTRIBUTING.md](../../CONTRIBUTING.md#adding-a-language-sdk).
