"""Browser-assisted credential capture over the Chrome DevTools Protocol (CDP).

Neither helper reads Chrome's encrypted cookie files on disk: that is fragile, OS-specific and
the wrong trust boundary. ``extract_from_debug_chrome`` talks to a Chrome the user deliberately
started with ``--remote-debugging-port``; for a dedicated sign-in window see
:func:`gabriel.core.sign_in.sign_in_with_browser`.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any
from urllib.parse import unquote

import httpx

from ..core.auth import AuthDeps, exchange_cookie
from ..core.metadata import DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS
from ..errors import GabrielError

_SLACK_DOMAIN = re.compile(r"(^|\.)slack\.com$")


def _find_session_cookie(cookies: Any) -> str | None:
    for c in cookies if isinstance(cookies, list) else []:
        if not isinstance(c, dict) or c.get("name") != "d":
            continue
        domain = str(c.get("domain", "")).lstrip(".")
        value = str(c.get("value", ""))
        if _SLACK_DOMAIN.search(domain) and unquote(value).startswith("xoxd-"):
            return value
    return None


async def _cdp_call(ws: Any, id: int, method: str, timeout: float = 10.0) -> dict[str, Any]:
    await ws.send(json.dumps({"id": id, "method": method}))
    async with asyncio.timeout(timeout):
        while True:
            try:
                msg = json.loads(await ws.recv())
            except ValueError:
                continue
            if msg.get("id") != id:
                continue
            if msg.get("error"):
                raise GabrielError("INVALID_STATE", f"CDP {method} failed: {msg['error'].get('message')}")
            result = msg.get("result")
            return result if isinstance(result, dict) else {}


async def _debugger_url(http: httpx.AsyncClient, port: int) -> str:
    info = (await http.get(f"http://127.0.0.1:{port}/json/version")).json()
    url = info.get("webSocketDebuggerUrl") if isinstance(info, dict) else None
    if not url:
        raise GabrielError("INVALID_STATE", "no webSocketDebuggerUrl")
    return str(url)


def _deps(http: httpx.AsyncClient) -> AuthDeps:
    return AuthDeps(http_client=http, metadata=DEFAULT_CLIENT_METADATA, endpoints=DEFAULT_ENDPOINTS)


async def extract_from_debug_chrome(port: int) -> str:
    """Reads the ``.slack.com`` ``d`` cookie from a Chrome started with a remote debugging port.

    Start Chrome like this first, sign in, then run ``gabriel auth extract``::

        Google Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/slack-profile
    """
    from websockets.asyncio.client import connect

    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise GabrielError("INVALID_ARGUMENT", "debug port must be 1–65535")
    async with httpx.AsyncClient(timeout=15.0) as http:
        try:
            ws_url = await _debugger_url(http, port)
        except Exception:
            raise GabrielError(
                "INVALID_STATE",
                f"no Chrome DevTools endpoint on 127.0.0.1:{port}. Start Chrome with "
                f"--remote-debugging-port={port} --user-data-dir=<dir>, sign in to Slack, then retry.",
            ) from None
        async with connect(ws_url, max_size=None) as ws:
            result = await _cdp_call(ws, 1, "Storage.getCookies")
        cookie = _find_session_cookie(result.get("cookies"))
        if cookie is None:
            raise GabrielError(
                "INVALID_STATE",
                "no signed-in `.slack.com` session cookie found in that Chrome; sign in to Slack there first",
            )
        # Verify the cookie is live before handing it back.
        await exchange_cookie(_deps(http), cookie)
        return cookie
