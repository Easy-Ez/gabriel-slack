"""Browser-assisted credential capture over the Chrome DevTools Protocol (CDP).

Neither helper reads Chrome's encrypted cookie files on disk: that is fragile, OS-specific and
the wrong trust boundary. ``browser_login`` opens a dedicated window with a fresh temporary
profile; ``extract_from_debug_chrome`` talks to a Chrome the user deliberately started with
``--remote-debugging-port``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import re
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx

from ..core.auth import AuthDeps, exchange_cookie, find_team
from ..core.metadata import DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS
from ..errors import GabrielError

_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

_SLACK_DOMAIN = re.compile(r"(^|\.)slack\.com$")
_WORKSPACE_URL = re.compile(r"^https://[a-z0-9-]+\.slack\.com/?$", re.IGNORECASE)


def _find_chrome() -> str:
    explicit = os.environ.get("GABRIEL_CHROME_PATH")
    if explicit:
        if not Path(explicit).exists():
            raise GabrielError("INVALID_ARGUMENT", "GABRIEL_CHROME_PATH does not exist")
        return explicit
    for candidate in _CHROME_CANDIDATES:
        if Path(candidate).exists():
            return candidate
    raise GabrielError("INVALID_STATE", "Chrome/Chromium not found; set GABRIEL_CHROME_PATH")


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


def _stderr(line: str) -> None:
    print(line, file=sys.stderr)


async def browser_login(domain: str | None, *, timeout: float = 300.0, log: Callable[[str], None] = _stderr) -> str:
    """Opens a dedicated Chrome window (fresh temporary profile) on the workspace sign-in page.

    The person signs in themselves; once the ``.slack.com`` ``d`` cookie can be exchanged for a
    workspace token, the window closes and the cookie is returned. The cookie is never printed.
    The temporary profile is deleted afterwards.
    """
    from websockets.asyncio.client import connect

    async with httpx.AsyncClient(timeout=15.0) as http:
        login_url = "https://slack.com/signin"
        if domain:
            try:
                team = await find_team(_deps(http), domain)
            except Exception:
                team = {}
            url = team.get("url")
            if team.get("ok") is True and isinstance(url, str) and _WORKSPACE_URL.match(url):
                login_url = url
        chrome = _find_chrome()
        profile = tempfile.mkdtemp(prefix="gabriel-login-")
        port = 19222 + random.randrange(1000)
        log(f"Opening a sign-in window at {login_url} — complete the Slack sign-in there; it closes automatically.")
        proc = await asyncio.create_subprocess_exec(
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            f"--app={login_url}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            browser_ws_url = ""
            for _ in range(50):
                try:
                    browser_ws_url = await _debugger_url(http, port)
                    break
                except Exception:
                    await asyncio.sleep(0.3)
            if not browser_ws_url:
                raise GabrielError("INVALID_STATE", "could not reach the sign-in browser window")
            async with connect(browser_ws_url, max_size=None) as ws:
                deadline = time.monotonic() + timeout
                seq = 1
                while time.monotonic() < deadline:
                    if proc.returncode is not None:
                        raise GabrielError("CANCELLED", "the sign-in window was closed before signing in")
                    result = await _cdp_call(ws, seq, "Storage.getCookies")
                    seq += 1
                    cookie = _find_session_cookie(result.get("cookies"))
                    if cookie is not None:
                        try:
                            await exchange_cookie(_deps(http), cookie)
                            return cookie
                        except GabrielError:
                            pass  # still redirecting through SSO; keep waiting
                    await asyncio.sleep(1.0)
            raise GabrielError("NETWORK_TIMEOUT", "timed out waiting for sign-in")
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.terminate()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(proc.wait(), 5.0)
            shutil.rmtree(profile, ignore_errors=True)
