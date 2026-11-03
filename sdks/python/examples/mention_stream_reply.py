"""Quickstart: reply with a streamed answer whenever someone @-mentions you.

    GABRIEL_COOKIE_D=xoxd-... GABRIEL_WORKSPACE=your-workspace uv run python examples/mention_stream_reply.py

Replace ``fake_llm`` with your own LLM client. Pass ``signal`` so generation stops when the
triggering message (or the streamed reply) is deleted.
"""

from __future__ import annotations

import asyncio
import re
import sys
from collections.abc import AsyncIterator
from typing import Any

from gabriel import AbortSignal, AuthDeadEvent, GabrielClient, MessageCreatedEvent, console_logger, load_session_cookie


async def fake_llm(prompt: str, signal: AbortSignal) -> AsyncIterator[str]:
    for word in re.split(r"(\s+)", f"You said:\n\n{prompt[:400]}"):
        if signal.aborted:
            return
        await asyncio.sleep(0.03)
        yield word


async def main() -> None:
    session = load_session_cookie()
    if session is None:
        sys.exit("Set GABRIEL_COOKIE_D or run `gabriel auth parse-curl --save` first.")
    client = GabrielClient.from_cookie(
        cookie_d=session.cookie_d, workspace=session.workspace, logger=console_logger("info")
    )

    @client.on_message(mentions_me=True)
    async def answer(evt: MessageCreatedEvent, ctx: Any) -> None:
        if evt.origin != "other_user":
            return  # ignore bots and your own messages
        stream = await ctx.reply_stream(initial_markdown="_🤖 thinking…_", preserve_unread_cursor=True)
        signal = AbortSignal.any(ctx.signal, stream.signal)
        async for chunk in fake_llm(evt.to_llm_context(), signal):
            stream.append(chunk)
        if not signal.aborted:
            await stream.finish()

    @client.on(AuthDeadEvent)
    def dead(evt: AuthDeadEvent, _ctx: Any) -> None:
        print(f"session expired for {evt.team_id}: {evt.reason}", file=sys.stderr)

    client.on_error(lambda err, evt: print("handler failed", evt.event_id if evt else None, err, file=sys.stderr))

    async with client:
        print(f"listening as {client.self_user_id} in {client.team_id} (Ctrl-C to stop)")
        await asyncio.Event().wait()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
