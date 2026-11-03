"""In-app sign-in: a dedicated browser window that hands back the session cookie.

Port of ``sdks/node/src/core/sign-in.ts``. The browser is driven over the Chrome DevTools
Protocol (CDP); Chrome's encrypted cookie files on disk are never read.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar
from urllib.parse import unquote

import httpx
from websockets.exceptions import ConnectionClosed

from ..errors import GabrielError, cancelled_error
from ..util.aio import AbortController, AbortSignal, race_signal, sleep
from ..util.redact import redact_secret
from .auth import AuthDeps, AuthTeam, exchange_cookie, find_team
from .metadata import DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS, ClientMetadata, Endpoints

T = TypeVar("T")

#: Progress of :func:`sign_in_with_browser`. ``verifying`` and ``waiting`` may alternate during SSO redirects.
SignInStatus = Literal["opening", "waiting", "verifying", "done"]


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkspaceInfo:
    """A workspace the session cookie is signed in to. Carries no credentials."""

    team_id: str
    domain: str
    name: str
    user_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class SignInResult:
    #: The ``.slack.com`` ``d`` session cookie (``xoxd-…``). Treat it like a password.
    cookie_d: str = field(repr=False)
    workspaces: list[WorkspaceInfo]

    def __repr__(self) -> str:
        return f"SignInResult(cookie_d={redact_secret(self.cookie_d)!r}, workspaces={self.workspaces!r})"


class BrowserProcess(Protocol):
    """Minimal child-process surface of the sign-in window (matches ``asyncio.subprocess.Process``)."""

    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


class CdpSocket(Protocol):
    """Minimal DevTools WebSocket surface (matches ``websockets`` ``ClientConnection``)."""

    async def send(self, message: str) -> None: ...

    async def recv(self) -> str | bytes: ...

    async def close(self) -> None: ...


async def _spawn(command: str, args: list[str]) -> BrowserProcess:
    return await asyncio.create_subprocess_exec(
        command,
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )


async def _connect(url: str) -> CdpSocket:
    from websockets.asyncio.client import connect

    return await connect(url, max_size=None, compression=None)


def _read_file(file: str) -> str | None:
    try:
        return Path(file).read_text(encoding="utf-8")
    except OSError:
        return None


@dataclass(slots=True, kw_only=True)
class SignInDeps:
    """Side effects of the sign-in, injectable for tests."""

    #: HTTP client for ``auth.findTeam`` and the cookie exchange; one is created when omitted.
    http_client: httpx.AsyncClient | None = None
    metadata: ClientMetadata = DEFAULT_CLIENT_METADATA
    endpoints: Endpoints = DEFAULT_ENDPOINTS
    spawn: Callable[[str, list[str]], Awaitable[BrowserProcess]] = _spawn
    connect: Callable[[str], Awaitable[CdpSocket]] = _connect
    exists: Callable[[str], bool] = os.path.exists
    #: Creates a fresh private temporary directory whose name starts with the prefix.
    mkdtemp: Callable[[str], str] = lambda prefix: tempfile.mkdtemp(prefix=prefix)
    #: File contents, or ``None`` when it does not exist (yet).
    read_file: Callable[[str], str | None] = _read_file
    rmtree: Callable[[str], None] = lambda path: shutil.rmtree(path, ignore_errors=True)
    #: Cookie poll interval.
    poll_interval: float = 1.0
    #: How long a signed-in-looking cookie may keep failing the exchange (SSO redirects) before it counts as invalid.
    rejected_grace: float = 30.0
    #: Limit for the browser to start and accept the DevTools connection.
    launch_timeout: float = 15.0
    #: How long to wait for the browser to exit after terminate() before kill().
    exit_wait: float = 3.0


_CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
]

_DEFAULT_SIGN_IN_URL = "https://slack.com/signin"
_CDP_CALL_TIMEOUT = 10.0
_EXCHANGE_TIMEOUT = 15.0
_SLACK_DOMAIN = re.compile(r"(^|\.)slack\.com$")
_WORKSPACE_URL = re.compile(r"^https://[a-z0-9-]+\.slack\.com/?$", re.IGNORECASE)
_DEVTOOLS_PATH = re.compile(r"/devtools/browser/[\w-]+")


def _to_workspace_info(team: AuthTeam) -> WorkspaceInfo:
    return WorkspaceInfo(team_id=team.id, domain=team.domain, name=team.name, user_id=team.user_id)


async def _exchange(
    deps: SignInDeps, http: httpx.AsyncClient, cookie_d: str, signal: AbortSignal | None
) -> list[WorkspaceInfo]:
    auth = AuthDeps(http_client=http, metadata=deps.metadata, endpoints=deps.endpoints)
    combined = AbortSignal.any(signal, AbortSignal.timeout(_EXCHANGE_TIMEOUT))
    try:
        result = await exchange_cookie(auth, cookie_d, combined)
    finally:
        combined.dispose()
    return [_to_workspace_info(t) for t in result.teams]


async def list_workspaces(cookie_d: str, *, signal: AbortSignal | None = None) -> list[WorkspaceInfo]:
    """Lists the workspaces a session cookie is signed in to.

    Verifies the cookie with one exchange; the workspace tokens it yields are dropped, never
    returned or logged. Raises ``AUTH_DEAD`` when the cookie is no longer signed in.
    """
    return await list_workspaces_with(SignInDeps(), cookie_d, signal=signal)


async def list_workspaces_with(
    deps: SignInDeps, cookie_d: str, *, signal: AbortSignal | None = None
) -> list[WorkspaceInfo]:
    """:func:`list_workspaces` with injected dependencies."""
    if not isinstance(cookie_d, str) or len(cookie_d.strip()) < 8:
        raise GabrielError("INVALID_ARGUMENT", "cookie_d is required (the `d` session cookie)")
    if signal is not None:
        signal.throw_if_aborted()
    try:
        if deps.http_client is not None:
            return await _exchange(deps, deps.http_client, cookie_d, signal)
        async with httpx.AsyncClient(timeout=_EXCHANGE_TIMEOUT) as http:
            return await _exchange(deps, http, cookie_d, signal)
    except GabrielError:
        if signal is not None and signal.aborted:
            raise cancelled_error(signal.reason) from None
        raise


async def sign_in_with_browser(
    *,
    workspace: str | None = None,
    timeout: float = 300.0,
    signal: AbortSignal | None = None,
    browser_path: str | None = None,
    on_status: Callable[[SignInStatus], None] | None = None,
) -> SignInResult:
    """Signs in to Slack in a dedicated Chrome window (fresh temporary profile).

    The person signs in themselves; once the ``.slack.com`` ``d`` cookie exchanges for a
    workspace token, the window closes and the cookie is returned with the signed-in workspaces.

    Writes nothing to disk besides the temporary profile, which is deleted whatever the outcome,
    and prints nothing: progress is reported through ``on_status`` only. Errors never contain the
    cookie or a token.

    Args:
        workspace: Workspace subdomain (``your-workspace``): the window opens on its sign-in page.
            Default: ``https://slack.com/signin``.
        timeout: Limit for the whole sign-in, in seconds.
        signal: Aborting closes the window and raises ``CANCELLED``. Task cancellation works too.
        browser_path: Absolute path of a Chrome / Chromium executable. Default: the first one found
            in the usual install locations.
        on_status: Progress callback. Exceptions it raises are ignored.

    Raises:
        GabrielError: ``BROWSER_NOT_FOUND`` (no Chrome / Chromium), ``SIGN_IN_TIMEOUT``, ``CANCELLED``
            (``signal`` aborted or the window was closed) or ``AUTH_DEAD`` (signed in, but Slack
            rejects the session cookie).
    """
    return await sign_in_with_browser_with(
        SignInDeps(),
        workspace=workspace,
        timeout=timeout,
        signal=signal,
        browser_path=browser_path,
        on_status=on_status,
    )


def _resolve_browser(browser_path: str | None, exists: Callable[[str], bool]) -> str:
    if browser_path is not None:
        if not isinstance(browser_path, str) or not os.path.isabs(browser_path):
            raise GabrielError("INVALID_ARGUMENT", "browser_path must be an absolute path")
        if not exists(browser_path):
            raise GabrielError("BROWSER_NOT_FOUND", "browser_path does not exist")
        return browser_path
    for candidate in _CHROME_CANDIDATES:
        if exists(candidate):
            return candidate
    raise GabrielError("BROWSER_NOT_FOUND", "Chrome or Chromium not found; pass browser_path")


async def _sign_in_url(deps: SignInDeps, http: httpx.AsyncClient, workspace: str | None) -> str:
    if not workspace:
        return _DEFAULT_SIGN_IN_URL
    try:
        team = await find_team(AuthDeps(http_client=http, metadata=deps.metadata, endpoints=deps.endpoints), workspace)
    except GabrielError:
        return _DEFAULT_SIGN_IN_URL
    url = team.get("url")
    if team.get("ok") is True and isinstance(url, str) and _WORKSPACE_URL.match(url):
        return url
    return _DEFAULT_SIGN_IN_URL


async def _wait_for_devtools(deps: SignInDeps, profile: str, signal: AbortSignal) -> str:
    """Reads the DevTools endpoint Chrome writes into the profile when started with ``--remote-debugging-port=0``."""
    deadline = time.monotonic() + deps.launch_timeout
    while time.monotonic() < deadline:
        lines = [
            line.strip() for line in (deps.read_file(os.path.join(profile, "DevToolsActivePort")) or "").split("\n")
        ]
        port, ws_path = [*lines, "", ""][:2]
        if re.fullmatch(r"\d{1,5}", port) and int(port) <= 65535 and _DEVTOOLS_PATH.fullmatch(ws_path):
            return f"ws://127.0.0.1:{port}{ws_path}"
        await sleep(0.1, signal)
    raise GabrielError("INVALID_STATE", "could not reach the sign-in browser window")


def _session_cookie(cookies: Any) -> str | None:
    for c in cookies if isinstance(cookies, list) else []:
        if not isinstance(c, dict) or c.get("name") != "d":
            continue
        domain, value = c.get("domain"), c.get("value")
        if not isinstance(domain, str) or not isinstance(value, str):
            continue
        if _SLACK_DOMAIN.search(domain.lstrip(".")) and unquote(value).startswith("xoxd-"):
            return value
    return None


def _window_closed() -> GabrielError:
    return GabrielError("CANCELLED", "the sign-in window was closed before signing in")


def _cookie_rejected() -> GabrielError:
    return GabrielError("AUTH_DEAD", "signed in, but Slack rejected the session cookie")


class _Cdp:
    """Sequential CDP calls on the browser-level DevTools socket."""

    def __init__(self, socket: CdpSocket) -> None:
        self._socket = socket
        self._seq = 0

    async def call(self, method: str) -> dict[str, Any]:
        self._seq += 1
        call_id = self._seq
        try:
            await self._socket.send(json.dumps({"id": call_id, "method": method}))
            async with asyncio.timeout(_CDP_CALL_TIMEOUT):
                while True:
                    try:
                        msg = json.loads(await self._socket.recv())
                    except ValueError:
                        continue
                    if not isinstance(msg, dict) or msg.get("id") != call_id:
                        continue
                    if msg.get("error"):
                        raise GabrielError("INVALID_STATE", f"the sign-in browser window rejected {method}")
                    result = msg.get("result")
                    return result if isinstance(result, dict) else {}
        except ConnectionClosed:
            raise _window_closed() from None
        except TimeoutError:
            raise GabrielError("INVALID_STATE", f"the sign-in browser window did not answer {method}") from None


async def sign_in_with_browser_with(
    deps: SignInDeps,
    *,
    workspace: str | None = None,
    timeout: float = 300.0,
    signal: AbortSignal | None = None,
    browser_path: str | None = None,
    on_status: Callable[[SignInStatus], None] | None = None,
) -> SignInResult:
    """:func:`sign_in_with_browser` with injected dependencies."""
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 0 < timeout < float("inf"):
        raise GabrielError("INVALID_ARGUMENT", "timeout must be a positive number of seconds")
    if workspace is not None and (not isinstance(workspace, str) or len(workspace) > 255):
        raise GabrielError("INVALID_ARGUMENT", 'workspace must be a workspace subdomain like "your-workspace"')
    if signal is not None:
        signal.throw_if_aborted()
    browser = _resolve_browser(browser_path, deps.exists)
    if deps.http_client is not None:
        return await _run(deps, deps.http_client, browser, workspace, timeout, signal, on_status)
    async with httpx.AsyncClient(timeout=_EXCHANGE_TIMEOUT) as http:
        return await _run(deps, http, browser, workspace, timeout, signal, on_status)


async def _run(
    deps: SignInDeps,
    http: httpx.AsyncClient,
    browser: str,
    workspace: str | None,
    timeout: float,
    signal: AbortSignal | None,
    on_status: Callable[[SignInStatus], None] | None,
) -> SignInResult:
    last_status: list[SignInStatus] = []

    def report(status: SignInStatus) -> None:
        if last_status and last_status[-1] == status:
            return
        last_status.append(status)
        if on_status is not None:
            with contextlib.suppress(Exception):  # a failing progress callback must not break the sign-in
                on_status(status)

    # Every way the sign-in can end early (abort, timeout, window closed) aborts `stop` with its
    # error; each await below races against it.
    stop = AbortController()
    closing = False
    rejected: tuple[str, float] | None = None

    def fail(err: GabrielError) -> None:
        if not closing:
            stop.abort(err)

    def on_timeout() -> None:
        secs = round(timeout)
        fail(
            _cookie_rejected()
            if rejected
            else GabrielError("SIGN_IN_TIMEOUT", f"sign-in did not complete within {secs} s")
        )

    async def guard(awaitable: Awaitable[T]) -> T:
        if stop.signal.aborted:
            if asyncio.iscoroutine(awaitable):
                awaitable.close()  # never started; avoids "coroutine was never awaited"
            raise cancelled_error(stop.signal.reason)
        return await race_signal(awaitable, stop.signal)

    remove_abort = signal.add_listener(lambda: fail(cancelled_error(signal.reason))) if signal else (lambda: None)
    timer = asyncio.get_running_loop().call_later(timeout, on_timeout)

    profile: str | None = None
    proc: BrowserProcess | None = None
    exit_watch: asyncio.Task[int] | None = None
    socket: CdpSocket | None = None
    try:
        report("opening")
        login_url = await guard(_sign_in_url(deps, http, workspace))
        profile = deps.mkdtemp("gabriel-login-")
        try:
            proc = await guard(
                deps.spawn(
                    browser,
                    [
                        "--remote-debugging-port=0",
                        f"--user-data-dir={profile}",
                        "--no-first-run",
                        "--no-default-browser-check",
                        f"--app={login_url}",
                    ],
                )
            )
        except OSError as err:
            raise GabrielError("BROWSER_NOT_FOUND", "the browser could not be started", cause=err) from None
        exit_watch = asyncio.ensure_future(proc.wait())
        exit_watch.add_done_callback(lambda _t: fail(_window_closed()))
        ws_url = await guard(_wait_for_devtools(deps, profile, stop.signal))
        try:
            socket = await guard(_within(deps.launch_timeout, deps.connect(ws_url)))
        except (OSError, TimeoutError) as err:
            raise GabrielError("INVALID_STATE", "could not reach the sign-in browser window", cause=err) from None
        cdp = _Cdp(socket)

        report("waiting")
        saw_window = False
        while True:
            # On macOS closing the last window leaves Chrome running, so watch the targets too.
            targets = (await guard(cdp.call("Target.getTargets"))).get("targetInfos")
            windows = (
                [t for t in targets if isinstance(t, dict) and t.get("type") in ("page", "app")]
                if isinstance(targets, list)
                else []
            )
            if windows:
                saw_window = True
            elif saw_window:
                raise _window_closed()

            cookie_d = _session_cookie((await guard(cdp.call("Storage.getCookies"))).get("cookies"))
            if cookie_d is not None:
                report("verifying")
                try:
                    workspaces = await guard(_exchange(deps, http, cookie_d, stop.signal))
                    result = SignInResult(cookie_d=cookie_d, workspaces=workspaces)
                    break
                except GabrielError as err:
                    if stop.signal.aborted:
                        raise cancelled_error(stop.signal.reason) from None
                    if err.code == "AUTH_DEAD":
                        # Usually the sign-in is still redirecting through SSO; give it time before giving up.
                        if rejected is None or rejected[0] != cookie_d:
                            rejected = (cookie_d, time.monotonic())
                        elif time.monotonic() - rejected[1] >= deps.rejected_grace:
                            raise _cookie_rejected() from None
                    elif not err.retryable:
                        raise
                report("waiting")
            else:
                rejected = None
            await sleep(deps.poll_interval, stop.signal)
    except GabrielError:
        if stop.signal.aborted:
            raise cancelled_error(stop.signal.reason) from None
        raise
    except asyncio.CancelledError:
        raise
    except Exception as err:
        if stop.signal.aborted:
            raise cancelled_error(stop.signal.reason) from None
        raise GabrielError("INVALID_STATE", "browser sign-in failed", cause=err) from None
    finally:
        closing = True
        timer.cancel()
        remove_abort()
        await _close_window(deps, proc, exit_watch, socket)
        if profile is not None:
            with contextlib.suppress(Exception):  # best effort; the OS cleans its temp directory eventually
                deps.rmtree(profile)
    report("done")
    return result


async def _within(seconds: float, awaitable: Awaitable[T]) -> T:
    """``await`` with a time limit, raising ``TimeoutError``.

    Not ``asyncio.wait_for``: on Python 3.11 it swallows a task cancellation that arrives as the
    awaitable completes, which would leave the sign-in window open until the overall timeout.
    """
    async with asyncio.timeout(seconds):
        return await awaitable


async def _close_window(
    deps: SignInDeps, proc: BrowserProcess | None, exit_watch: asyncio.Task[int] | None, socket: CdpSocket | None
) -> None:
    if socket is not None:
        with contextlib.suppress(Exception):
            await socket.close()
    if proc is None:
        return
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await _within(deps.exit_wait, asyncio.shield(proc.wait()))
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(TimeoutError):
                await _within(deps.exit_wait, asyncio.shield(proc.wait()))
    if exit_watch is not None and not exit_watch.done():
        exit_watch.cancel()
