"""In-app sign-in (port of sdks/node/test/sign-in.test.ts). No network, no real browser."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from websockets.exceptions import ConnectionClosedOK

from gabriel.core.sign_in import (
    SignInDeps,
    SignInStatus,
    WorkspaceInfo,
    list_workspaces_with,
    sign_in_with_browser_with,
)
from gabriel.errors import GabrielError
from gabriel.util.aio import AbortController
from harness import ENDPOINTS

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
COOKIE = "xoxd-test-cookie-value"
TOKEN = "xoxc-test-token-1"
WORKSPACE = WorkspaceInfo(team_id="T0123456789", domain="your-workspace", name="Your Workspace", user_id="U0ALICE0001")
INFINITE = 10**9


class FakeProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.signals: list[str] = []
        self.ignore_terminate = False
        self._exited = asyncio.Event()

    def exit(self, code: int) -> None:
        if self.returncode is None:
            self.returncode = code
            self._exited.set()

    def terminate(self) -> None:
        self.signals.append("terminate")
        if not self.ignore_terminate:
            asyncio.get_running_loop().call_soon(self.exit, -15)

    def kill(self) -> None:
        self.signals.append("kill")
        asyncio.get_running_loop().call_soon(self.exit, -9)

    async def wait(self) -> int:
        await self._exited.wait()
        assert self.returncode is not None
        return self.returncode


class FakeDevTools:
    """Chrome's browser-level DevTools endpoint: answers ``Target.getTargets`` and ``Storage.getCookies``."""

    def __init__(self, url: str, browser: FakeBrowser) -> None:
        self.url = url
        self.closed = False
        self.methods: list[str] = []
        self._browser = browser
        self._inbox: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, message: str) -> None:
        if self.closed:
            raise ConnectionClosedOK(None, None)
        msg = json.loads(message)
        self.methods.append(msg["method"])
        if msg["method"] == "Target.getTargets":
            result: dict[str, Any] = {"targetInfos": [{"type": "page"}] * self._browser.windows}
        else:
            result = {"cookies": self._browser.cookies()}
        self._inbox.put_nowait("not json")
        self._inbox.put_nowait(json.dumps({"id": msg["id"], "result": result}))

    async def recv(self) -> str:
        item = await self._inbox.get()
        if item is None:
            raise ConnectionClosedOK(None, None)
        return item

    async def close(self) -> None:
        self.drop()

    def drop(self) -> None:
        """The connection goes away (browser quit or crashed)."""
        if not self.closed:
            self.closed = True
            self._inbox.put_nowait(None)


class FakeBrowser:
    """In-memory browser, file system and Slack endpoints for the sign-in flow."""

    def __init__(self) -> None:
        self.installed = {CHROME}
        self.files: dict[str, str] = {}
        self.profiles: list[str] = []
        self.removed: list[str] = []
        self.spawns: list[tuple[str, list[str]]] = []
        self.sockets: list[FakeDevTools] = []
        self.auth_cookies: list[str] = []
        self.process: FakeProcess | None = None
        self.connected = asyncio.Event()
        self.windows = 1
        #: Cookie polls before the ``d`` cookie appears; INFINITE never signs in.
        self.sign_in_after_polls = 2
        #: Cookie exchanges answered with a sign-in redirect before succeeding.
        self.reject_exchanges = 0
        self.write_devtools_port = True
        self.spawn_error: OSError | None = None
        self._polls = 0
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self._dispatch))

    def cookies(self) -> list[dict[str, str]]:
        self._polls += 1
        base = [{"name": "b", "domain": ".slack.com", "value": "anonymous"}]
        if self._polls > self.sign_in_after_polls:
            base.append({"name": "d", "domain": ".slack.com", "value": COOKIE})
        return base

    @property
    def profile(self) -> str:
        return self.profiles[0]

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "slack.test" and request.url.path == "/api/auth.findTeam":
            return httpx.Response(200, json={"ok": True, "url": "https://your-workspace.slack.com/"})
        if request.url.host == "app.slack.test":
            self.auth_cookies.append(request.headers.get("cookie", ""))
            if self.reject_exchanges > 0:
                self.reject_exchanges -= 1
                return httpx.Response(302, headers={"location": "https://slack.test/signin"})
            team = {
                "id": "T0123456789",
                "name": "Your Workspace",
                "domain": "your-workspace",
                "url": "https://your-workspace.slack.com/",
                "token": TOKEN,
                "user_id": "U0ALICE0001",
            }
            return httpx.Response(200, json={"teams": {"T0123456789": team}})
        raise AssertionError(f"unexpected request {request.url}")

    async def _spawn(self, command: str, args: list[str]) -> FakeProcess:
        self.spawns.append((command, args))
        if self.spawn_error is not None:
            raise self.spawn_error
        proc = FakeProcess()
        self.process = proc
        if self.write_devtools_port:
            profile = next(a for a in args if a.startswith("--user-data-dir=")).removeprefix("--user-data-dir=")
            port_file = os.path.join(profile, "DevToolsActivePort")
            asyncio.get_running_loop().call_later(
                0.005, self.files.__setitem__, port_file, "51234\n/devtools/browser/0a1b2c3d-test\n"
            )
        return proc

    async def _connect(self, url: str) -> FakeDevTools:
        socket = FakeDevTools(url, self)
        self.sockets.append(socket)
        self.connected.set()
        return socket

    def _mkdtemp(self, prefix: str) -> str:
        path = f"/tmp/{prefix}{len(self.profiles)}"
        self.profiles.append(path)
        return path

    def deps(self, **overrides: Any) -> SignInDeps:
        values: dict[str, Any] = {
            "http_client": self.http,
            "endpoints": ENDPOINTS,
            "spawn": self._spawn,
            "connect": self._connect,
            "exists": lambda path: path in self.installed,
            "mkdtemp": self._mkdtemp,
            "read_file": self.files.get,
            "rmtree": self.removed.append,
            "poll_interval": 0.005,
            "rejected_grace": 60.0,
            "launch_timeout": 0.5,
            "exit_wait": 0.05,
        }
        values.update(overrides)
        return SignInDeps(**values)


@pytest.fixture
async def browser() -> AsyncIterator[FakeBrowser]:
    fake = FakeBrowser()
    yield fake
    await fake.http.aclose()


def assert_no_secrets(*texts: str) -> None:
    for text in texts:
        assert COOKIE not in text
        assert TOKEN not in text


def after_status(target: SignInStatus, delay: float, action: Any) -> Any:
    def on_status(status: SignInStatus) -> None:
        if status == target:
            asyncio.get_running_loop().call_later(delay, action)

    return on_status


async def test_returns_cookie_and_workspaces_closes_window_and_removes_profile(browser: FakeBrowser) -> None:
    statuses: list[SignInStatus] = []
    result = await sign_in_with_browser_with(browser.deps(), workspace="your-workspace", on_status=statuses.append)

    assert result.cookie_d == COOKIE
    assert result.workspaces == [WORKSPACE]
    assert_no_secrets(repr(result), repr(result.workspaces))
    assert statuses == ["opening", "waiting", "verifying", "done"]
    command, args = browser.spawns[0]
    assert command == CHROME
    assert args == [
        "--remote-debugging-port=0",
        f"--user-data-dir={browser.profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--app=https://your-workspace.slack.com/",
    ]
    assert browser.sockets[0].url == "ws://127.0.0.1:51234/devtools/browser/0a1b2c3d-test"
    assert browser.sockets[0].closed
    assert browser.process is not None and browser.process.signals == ["terminate"]
    assert browser.removed == [browser.profile]
    assert browser.auth_cookies == [f"d={COOKIE}"]


async def test_generic_sign_in_page_and_browser_path(browser: FakeBrowser) -> None:
    browser.installed = {"/opt/chromium/chrome"}
    await sign_in_with_browser_with(browser.deps(), browser_path="/opt/chromium/chrome")
    command, args = browser.spawns[0]
    assert command == "/opt/chromium/chrome"
    assert args[-1] == "--app=https://slack.com/signin"


async def test_keeps_waiting_while_sso_redirects(browser: FakeBrowser) -> None:
    browser.reject_exchanges = 2
    statuses: list[SignInStatus] = []
    result = await sign_in_with_browser_with(browser.deps(), on_status=statuses.append)
    assert result.workspaces == [WORKSPACE]
    assert len(browser.auth_cookies) == 3
    assert statuses == ["opening", "waiting", "verifying", "waiting", "verifying", "waiting", "verifying", "done"]


async def test_timeout_raises_sign_in_timeout_and_cleans_up(browser: FakeBrowser) -> None:
    browser.sign_in_after_polls = INFINITE
    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(browser.deps(), timeout=0.3)
    assert info.value.code == "SIGN_IN_TIMEOUT"
    assert browser.process is not None and browser.process.signals == ["terminate"]
    assert browser.sockets[0].closed
    assert browser.removed == [browser.profile]


async def test_abort_raises_cancelled_and_cleans_up(browser: FakeBrowser) -> None:
    browser.sign_in_after_polls = INFINITE
    ctrl = AbortController()
    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(
            browser.deps(), signal=ctrl.signal, on_status=after_status("waiting", 0.02, ctrl.abort)
        )
    assert info.value.code == "CANCELLED"
    assert browser.process is not None and browser.process.signals == ["terminate"]
    assert browser.removed == [browser.profile]


async def test_task_cancellation_cleans_up(browser: FakeBrowser) -> None:
    browser.sign_in_after_polls = INFINITE
    task = asyncio.ensure_future(sign_in_with_browser_with(browser.deps()))
    await browser.connected.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert browser.process is not None and browser.process.signals == ["terminate"]
    assert browser.removed == [browser.profile]


async def test_already_aborted_signal_opens_no_window(browser: FakeBrowser) -> None:
    ctrl = AbortController()
    ctrl.abort()
    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(browser.deps(), signal=ctrl.signal)
    assert info.value.code == "CANCELLED"
    assert browser.spawns == []
    assert browser.profiles == []


async def test_closing_the_window_raises_cancelled(browser: FakeBrowser) -> None:
    browser.sign_in_after_polls = INFINITE

    def close_window() -> None:
        browser.windows = 0

    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(browser.deps(), on_status=after_status("waiting", 0.02, close_window))
    assert info.value.code == "CANCELLED"
    assert browser.process is not None and browser.process.signals == ["terminate"]
    assert browser.removed == [browser.profile]


async def test_browser_exit_raises_cancelled(browser: FakeBrowser) -> None:
    browser.sign_in_after_polls = INFINITE

    def exit_browser() -> None:
        assert browser.process is not None
        browser.process.exit(0)

    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(browser.deps(), on_status=after_status("waiting", 0.02, exit_browser))
    assert info.value.code == "CANCELLED"
    assert browser.process is not None and browser.process.signals == []
    assert browser.removed == [browser.profile]


async def test_lost_devtools_connection_raises_cancelled(browser: FakeBrowser) -> None:
    browser.sign_in_after_polls = INFINITE

    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(
            browser.deps(), on_status=after_status("waiting", 0.02, lambda: browser.sockets[0].drop())
        )
    assert info.value.code == "CANCELLED"
    assert browser.removed == [browser.profile]


async def test_no_browser_installed_raises_browser_not_found(browser: FakeBrowser) -> None:
    browser.installed = set()
    statuses: list[SignInStatus] = []
    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(browser.deps(), on_status=statuses.append)
    assert info.value.code == "BROWSER_NOT_FOUND"
    assert statuses == []
    assert browser.spawns == []
    assert browser.profiles == []


async def test_browser_path_must_exist_and_be_absolute(browser: FakeBrowser) -> None:
    with pytest.raises(GabrielError) as missing:
        await sign_in_with_browser_with(browser.deps(), browser_path="/opt/missing/chrome")
    assert missing.value.code == "BROWSER_NOT_FOUND"
    with pytest.raises(GabrielError) as relative:
        await sign_in_with_browser_with(browser.deps(), browser_path="chrome")
    assert relative.value.code == "INVALID_ARGUMENT"
    assert browser.spawns == []


async def test_browser_that_cannot_start_raises_browser_not_found(browser: FakeBrowser) -> None:
    browser.spawn_error = FileNotFoundError(2, "No such file or directory")
    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(browser.deps())
    assert info.value.code == "BROWSER_NOT_FOUND"
    assert browser.removed == [browser.profile]


async def test_missing_devtools_endpoint_raises_invalid_state(browser: FakeBrowser) -> None:
    browser.write_devtools_port = False
    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(browser.deps(launch_timeout=0.05))
    assert info.value.code == "INVALID_STATE"
    assert browser.process is not None and browser.process.signals == ["terminate"]
    assert browser.removed == [browser.profile]


async def test_rejected_cookie_raises_auth_dead(browser: FakeBrowser) -> None:
    browser.reject_exchanges = INFINITE
    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(browser.deps(rejected_grace=0.03))
    assert info.value.code == "AUTH_DEAD"
    assert_no_secrets(str(info.value), repr(info.value))
    assert browser.removed == [browser.profile]


async def test_timeout_after_rejected_cookie_raises_auth_dead(browser: FakeBrowser) -> None:
    browser.reject_exchanges = INFINITE
    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(browser.deps(), timeout=0.3)
    assert info.value.code == "AUTH_DEAD"
    assert browser.removed == [browser.profile]


async def test_kills_a_browser_that_ignores_terminate(browser: FakeBrowser) -> None:
    spawn = browser._spawn

    async def stubborn(command: str, args: list[str]) -> FakeProcess:
        proc = await spawn(command, args)
        proc.ignore_terminate = True
        return proc

    await sign_in_with_browser_with(browser.deps(spawn=stubborn))
    assert browser.process is not None and browser.process.signals == ["terminate", "kill"]
    assert browser.removed == [browser.profile]


async def test_failing_on_status_is_ignored(browser: FakeBrowser) -> None:
    def broken(_status: SignInStatus) -> None:
        raise RuntimeError("ui crashed")

    result = await sign_in_with_browser_with(browser.deps(), on_status=broken)
    assert result.cookie_d == COOKIE


async def test_validates_options(browser: FakeBrowser) -> None:
    with pytest.raises(GabrielError) as info:
        await sign_in_with_browser_with(browser.deps(), timeout=0)
    assert info.value.code == "INVALID_ARGUMENT"
    assert browser.spawns == []


async def test_list_workspaces_returns_no_tokens(browser: FakeBrowser) -> None:
    workspaces = await list_workspaces_with(browser.deps(), COOKIE)
    assert workspaces == [WORKSPACE]
    assert_no_secrets(repr(workspaces))


async def test_list_workspaces_raises_auth_dead_for_signed_out_cookie(browser: FakeBrowser) -> None:
    browser.reject_exchanges = INFINITE
    with pytest.raises(GabrielError) as info:
        await list_workspaces_with(browser.deps(), COOKIE)
    assert info.value.code == "AUTH_DEAD"
    assert_no_secrets(str(info.value))


async def test_list_workspaces_raises_cancelled_when_aborted(browser: FakeBrowser) -> None:
    async def hang(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(10)
        raise AssertionError("not reached")

    ctrl = AbortController()
    async with httpx.AsyncClient(transport=httpx.MockTransport(hang)) as http:
        pending = asyncio.ensure_future(
            list_workspaces_with(browser.deps(http_client=http), COOKIE, signal=ctrl.signal)
        )
        await asyncio.sleep(0.01)
        ctrl.abort()
        with pytest.raises(GabrielError) as info:
            await pending
    assert info.value.code == "CANCELLED"


async def test_list_workspaces_rejects_missing_cookie(browser: FakeBrowser) -> None:
    with pytest.raises(GabrielError) as info:
        await list_workspaces_with(browser.deps(), "")
    assert info.value.code == "INVALID_ARGUMENT"
    assert browser.auth_cookies == []
