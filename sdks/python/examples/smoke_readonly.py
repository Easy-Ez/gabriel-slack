"""Read-only live smoke test: no messages, reactions, marks or uploads are written.

Verifies cookie exchange, WebSocket handshake, hydration, presence subscription, EdgeAPI / Web
API reads, then listens passively and reports event types (no content).

    GABRIEL_COOKIE_D=… GABRIEL_WORKSPACE=your-workspace uv run python examples/smoke_readonly.py [listen_seconds]
"""

from __future__ import annotations

import asyncio
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from gabriel import (
    BaseEvent,
    GabrielClient,
    GabrielError,
    LoopGuardOptions,
    MessageCreatedEvent,
    PresenceChangedEvent,
    ProtocolDriftEvent,
    load_session_cookie,
    redact_secret,
)


async def main() -> int:
    session = load_session_cookie()
    if session is None:
        print("No session: set GABRIEL_COOKIE_D or run `gabriel auth parse-curl --save`.", file=sys.stderr)
        return 2
    listen = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    results: list[tuple[str, bool, str]] = []

    async def check(name: str, fn: Callable[[], Awaitable[str]]) -> None:
        try:
            results.append((name, True, await fn()))
        except Exception as err:
            code = err.code if isinstance(err, GabrielError) else type(err).__name__
            results.append((name, False, f"{code}: {err}"))

    client = GabrielClient.from_cookie(
        cookie_d=session.cookie_d,
        workspace=session.workspace,
        loop_guard=LoopGuardOptions(include_self_messages=True),
    )
    counts: Counter[str] = Counter()
    seen: list[BaseEvent] = []
    events = client.events()

    async def collect() -> None:
        async for evt in events:
            counts[evt.type] += 1
            seen.append(evt)

    collector = asyncio.create_task(collect())

    async def start() -> str:
        await client.start()
        return f"team={client.team_id} self={client.self_user_id} ws={client.health().ws_state}"

    async def resolve_self() -> str:
        me = await client.users.resolve(client.self_user_id)
        return f"resolved (display_name {'present' if me and me.display_name else 'missing'})" if me else "not found"

    async def dnd() -> str:
        info = await client.users.get_dnd(client.self_user_id)
        return f"enabled={info.enabled}"

    async def counts_call() -> str:
        res = await client.core.call_web_api("client.counts", {})
        return f"channels={len(res.get('channels') or [])} ims={len(res.get('ims') or [])}"

    async def presence() -> str:
        got: asyncio.Future[str] = asyncio.get_running_loop().create_future()

        def on_presence(e: PresenceChangedEvent, _ctx: Any) -> None:
            if e.user.id == client.self_user_id and not got.done():
                got.set_result(f"presence={e.presence} initial={e.is_initial_snapshot}")

        off = client.on(PresenceChangedEvent, on_presence)
        try:
            await client.presence.watch([client.self_user_id])
            return await asyncio.wait_for(got, 10.0)
        finally:
            off()

    await check("start (cookie exchange + WebSocket hello)", start)
    await check("users.resolve(self) via EdgeAPI", resolve_self)
    await check("users.get_dnd(self) via dnd.teamInfo", dnd)
    await check("client.counts via core.call_web_api", counts_call)
    await check("presence.watch(self) initial snapshot", presence)

    print(f"listening passively for {listen:g}s…")
    await asyncio.sleep(listen)
    health = client.health()
    await client.stop(drain=2.0)
    collector.cancel()

    print("\n== read-only smoke results ==")
    for name, ok, detail in results:
        print(f"{'PASS' if ok else 'FAIL'}  {name} — {detail}")
    print(f"health before stop: {health.status} ws={health.ws_state} last_frame_age={health.last_frame_age:.1f}s")
    print(f"events observed: {dict(counts)}")
    drift = [
        f"{e.unknown_type}{'/' + e.unknown_subtype if e.unknown_subtype else ''}"
        for e in seen
        if isinstance(e, ProtocolDriftEvent)
    ]
    if drift:
        print(f"unknown frame types (protocol drift): {', '.join(drift)}")
    msg = next((e for e in seen if isinstance(e, MessageCreatedEvent)), None)
    if msg is not None:
        print(
            f"sample message.created shape: sender.display_name={'set' if msg.sender.display_name else 'unset'} "
            f"channel.name={'set' if msg.channel.name else 'unset'} origin={msg.origin} mentions={len(msg.mentions)}"
        )
    print(f"cookie used: {redact_secret(session.cookie_d)}")
    return 0 if all(ok for _, ok, _ in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
