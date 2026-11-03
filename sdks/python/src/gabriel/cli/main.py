"""``gabriel`` — credential helper for gabriel-slack (``python -m gabriel``)."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence

import httpx

from ..core.auth import AuthDeps, exchange_cookie
from ..core.metadata import DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS
from ..errors import GabrielError
from ..util.redact import redact_secret
from .session_store import delete_session, load_session_cookie, parse_curl_for_cookie, save_session, session_file

HELP = """gabriel — credential helper for gabriel-slack

Usage:
  gabriel auth login [domain]
      Open a dedicated Chrome window on the workspace sign-in page; after you sign in,
      the session cookie is verified and saved (0600). Nothing secret is printed.
  gabriel auth parse-curl [--save] [--workspace <domain>]
      Read a browser "Copy as cURL" request (any *.slack.com request) from stdin,
      extract the session cookie, verify it, and optionally save it (0600).
  gabriel auth extract [--port <n>] [--workspace <domain>]
      Read the session cookie from a Chrome you started with --remote-debugging-port
      (default 9222) and signed in to Slack, verify it, and save it (0600).
  gabriel auth status
      Verify the session from GABRIEL_COOKIE_D or the saved session and list workspaces.
  gabriel auth logout --yes
      Delete the saved session file.

Credentials are never printed; only prefixes and lengths are shown.
"""


def _read_stdin() -> str:
    if sys.stdin.isatty():
        print("Paste the cURL command, then press Ctrl-D:", file=sys.stderr)
    return sys.stdin.read()


async def _describe_session(cookie_d: str) -> None:
    async with httpx.AsyncClient() as http:
        deps = AuthDeps(http_client=http, metadata=DEFAULT_CLIENT_METADATA, endpoints=DEFAULT_ENDPOINTS)
        result = await exchange_cookie(deps, cookie_d)
    print(f"Session cookie {redact_secret(cookie_d)} is signed in to {len(result.teams)} workspace(s):")
    for t in result.teams:
        print(f"  - {t.domain} ({t.id}) user={t.user_id} token={redact_secret(t.token)}")


def _flag(args: Sequence[str], name: str) -> str | None:
    if name in args:
        i = list(args).index(name)
        return args[i + 1] if i + 1 < len(args) else None
    return None


async def run(argv: Sequence[str]) -> int:
    group = argv[0] if argv else None
    cmd = argv[1] if len(argv) > 1 else None
    rest = list(argv[2:])
    if group != "auth" or not cmd or cmd == "help" or "--help" in argv:
        print(HELP, end="")
        return 0 if group is None or "--help" in argv or cmd == "help" else 1
    if cmd == "parse-curl":
        cookie_d = parse_curl_for_cookie(_read_stdin())
        if not cookie_d:
            print("No `d=xoxd-…` cookie found in the input.", file=sys.stderr)
            return 1
        await _describe_session(cookie_d)
        if "--save" in rest:
            file = save_session(cookie_d, _flag(rest, "--workspace"))
            print(f"Saved to {file} (mode 0600).")
        return 0
    if cmd == "status":
        session = load_session_cookie()
        if session is None:
            print(
                f"No session: set GABRIEL_COOKIE_D or run `gabriel auth parse-curl --save` ({session_file()}).",
                file=sys.stderr,
            )
            return 1
        await _describe_session(session.cookie_d)
        return 0
    if cmd == "logout":
        if "--yes" not in rest:
            print(f"This deletes {session_file()}. Re-run with --yes to confirm.", file=sys.stderr)
            return 1
        print("Saved session deleted." if delete_session() else "No saved session.")
        return 0
    if cmd == "login":
        from .browser import browser_login

        domain = next((a for a in rest if not a.startswith("--")), None)
        cookie_d = await browser_login(domain)
        await _describe_session(cookie_d)
        file = save_session(cookie_d, domain)
        print(f"Saved to {file} (mode 0600).")
        return 0
    if cmd == "extract":
        from .browser import extract_from_debug_chrome

        port_arg = _flag(rest, "--port")
        try:
            port = int(port_arg) if port_arg else 9222
        except ValueError:
            raise GabrielError("INVALID_ARGUMENT", "debug port must be 1–65535") from None
        cookie_d = await extract_from_debug_chrome(port)
        await _describe_session(cookie_d)
        file = save_session(cookie_d, _flag(rest, "--workspace"))
        print(f"Saved to {file} (mode 0600).")
        return 0
    print(HELP, end="")
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return asyncio.run(run(list(sys.argv[1:] if argv is None else argv)))
    except GabrielError as err:
        print(f"error: {err.code}: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
