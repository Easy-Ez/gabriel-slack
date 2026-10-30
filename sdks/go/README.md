# Gabriel for Go (planned)

Module path (planned): `wecando.cc/gabriel-slack/sdks/go` — a vanity import path served from `wecando.cc`
(a `go-import` meta tag pointing at `github.com/Easy-Ez/gabriel-slack`).

The Go SDK will implement the same contract as the TypeScript reference SDK:

* Behavior: [`spec/behavior.md`](../../spec/behavior.md)
* Event payloads: [`spec/schema/events.schema.json`](../../spec/schema/events.schema.json)
* Conformance: every case in [`spec/fixtures/normalize`](../../spec/fixtures/normalize) must pass.

## Planned API shape

| Concept | Go idiom |
| :--- | :--- |
| Lifecycle | `err := client.Run(ctx)` — graceful shutdown when `ctx` is done |
| Push events | `client.OnMessageCreated(func(ctx context.Context, evt *gabriel.MessageCreated, r gabriel.Responder) error)` |
| Pull events | `for evt := range client.Events(ctx)` |
| Cancellation | `context.Context` (`ctx.signal` in TypeScript) |
| Options | functional options: `gabriel.WithWorkspace("your-workspace")`, `gabriel.WithLimit(50)` |
| Batch lookups | `Users.ResolveOne(ctx, id)` / `Users.ResolveMany(ctx, ids...)` |
| Streaming reply | `Chat.CreateStream(...)` returns an `io.WriteCloser` (`Close` = finish) |
| Errors | `*gabriel.Error` with `Code`, `Retryable`; `errors.Is(err, gabriel.ErrAuthDead)` |

The local multiplexing daemon (`gabriel-daemon`, one Slack connection shared by several agent
processes) is planned in Go as well.

Interested in contributing? See [CONTRIBUTING.md](../../CONTRIBUTING.md#adding-a-language-sdk).
