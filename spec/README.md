# Gabriel Specification

The language-neutral contract every Gabriel SDK implements. The TypeScript SDK in
[`sdks/node`](../sdks/node) is the reference implementation; Go and Python SDKs must
match it observably.

| Path | What it defines |
| :--- | :--- |
| [`behavior.md`](./behavior.md) | Normative behavior: layering, connection state machine, limits, keys, ordering, recovery, sending, errors. |
| [`schema/events.schema.json`](./schema/events.schema.json) | JSON Schema for every high-level event payload (pure, serializable data). |
| [`fixtures/normalize/`](./fixtures/normalize) | Conformance fixtures: a raw WebSocket frame and the normalized event it must produce. |

## Conformance fixtures

Each file in `fixtures/normalize/` is one case:

```jsonc
{
  "description": "what the case proves",
  "context": {
    "selfUserId": "U0SELF00001",           // the signed-in user
    "mySubteams": ["S0ONCALL001"],          // user groups the signed-in user belongs to
    "entities": {                           // names available to the renderer
      "users": { "U0ALICE0001": "Alice" },
      "channels": { "C0GENERAL01": "general" },
      "usergroups": { "S0ONCALL001": "oncall" }
    }
  },
  "frame": { "type": "message", "...": "raw downstream frame" },
  "expected": { "type": "message.created", "...": "subset of the serialized event" }  // or null = no event
}
```

An SDK passes a case when normalizing `frame` with `context` yields an event whose JSON
serialization **contains** `expected` (subset match), or yields no event when
`expected` is `null`. Origin classification in fixtures uses the stateless rules
(no record of messages sent by this client).

Run the reference suite with `pnpm test` from the repository root.

## Rules for fixtures

* Use placeholder IDs only: `X0123456789`-style or `X0NAME0001`-style (`U0ALICE0001`,
  `C0GENERAL01`). `pnpm oss:check` rejects anything that looks like a real ID.
* Never include tokens, cookies, reconnect tickets, emails or real message content.
* One behavior per file; name files after the behavior (`message-deleted.json`).
