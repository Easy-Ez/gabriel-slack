"""Escape hatch: log every raw frame type and call an unwrapped Web API method.

Raw access bypasses loop guards, the scope sandbox and origin detection.

    GABRIEL_COOKIE_D=xoxd-... uv run python examples/raw_core.py
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from gabriel import GabrielClient, ProtocolDriftEvent, RawWsFrame, load_session_cookie


async def main() -> None:
    session = load_session_cookie()
    if session is None:
        sys.exit("Set GABRIEL_COOKIE_D first.")
    client = GabrielClient.from_cookie(cookie_d=session.cookie_d, workspace=session.workspace)

    def log_frame(frame: RawWsFrame) -> None:
        if frame.get("type") != "pong":
            print("frame", frame.get("type"), frame.get("subtype") or "")

    client.core.on_raw_frame(log_frame)

    @client.on(ProtocolDriftEvent)
    def drift(evt: ProtocolDriftEvent, _ctx: Any) -> None:
        print("unknown frame type", evt.unknown_type, evt.unknown_subtype or "", file=sys.stderr)

    async with client:
        prefs = await client.core.call_web_api("users.prefs.get", {})
        print("prefs keys:", len(prefs.get("prefs") or {}))
        await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
