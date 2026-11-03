"""CLI credential helpers (port of sdks/node/test/cli.test.ts)."""

from __future__ import annotations

import stat
from pathlib import Path
from typing import Any

import httpx
import pytest

from gabriel.cli import main as cli_main
from gabriel.cli.main import main
from gabriel.cli.session_store import (
    delete_session,
    load_session,
    load_session_cookie,
    parse_curl_for_cookie,
    save_session,
)
from gabriel.core.sign_in import SignInResult
from gabriel.errors import GabrielError


def test_parse_curl_finds_d_cookie_in_header_and_flag_forms() -> None:
    curl = "curl 'https://x.slack.com/api/a' -H 'cookie: b=1; d=xoxd-abc%2Bdef; d-s=1'"
    assert parse_curl_for_cookie(curl) == "xoxd-abc%2Bdef"
    assert parse_curl_for_cookie('curl "https://x" -b "lc=1; d=xoxd-zzz"') == "xoxd-zzz"
    assert parse_curl_for_cookie('curl "https://x" -H "Cookie: other=1"') is None


@pytest.fixture
def config_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GABRIEL_CONFIG_DIR", str(tmp_path / "cfg"))
    monkeypatch.delenv("GABRIEL_COOKIE_D", raising=False)
    monkeypatch.delenv("GABRIEL_WORKSPACE", raising=False)
    return tmp_path / "cfg"


def test_session_store_writes_0600_and_reads_back(config_dir: Path) -> None:
    file = save_session("xoxd-test-cookie-value", "your-workspace")
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    stored = load_session()
    assert stored is not None
    assert (stored.cookie_d, stored.workspace) == ("xoxd-test-cookie-value", "your-workspace")
    assert "xoxd-test-cookie-value" not in repr(stored)
    # Same JSON shape as the Node CLI.
    assert '"cookieD"' in file.read_text()


def test_session_store_refuses_group_readable_file(config_dir: Path) -> None:
    file = save_session("xoxd-test-cookie-value")
    file.chmod(0o644)
    with pytest.raises(GabrielError) as info:
        load_session()
    assert info.value.code == "PERMISSION_DENIED"


def test_load_session_cookie_prefers_environment(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    save_session("xoxd-test-saved-value")
    monkeypatch.setenv("GABRIEL_COOKIE_D", "xoxd-test-env-value")
    monkeypatch.setenv("GABRIEL_WORKSPACE", "your-workspace")
    found = load_session_cookie()
    assert found is not None
    assert (found.cookie_d, found.workspace) == ("xoxd-test-env-value", "your-workspace")
    monkeypatch.delenv("GABRIEL_COOKIE_D")
    found = load_session_cookie()
    assert found is not None and found.cookie_d == "xoxd-test-saved-value"


def test_logout_requires_confirmation(config_dir: Path, capsys: pytest.CaptureFixture[str]) -> None:
    save_session("xoxd-test-cookie-value")
    assert main(["auth", "logout"]) == 1
    assert load_session() is not None
    assert main(["auth", "logout", "--yes"]) == 0
    assert load_session() is None
    assert delete_session() is False
    assert "deleted" in capsys.readouterr().out


def test_help_and_unknown_commands(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert main(["auth", "--help"]) == 0
    assert main(["nope"]) == 1
    assert "credential helper" in capsys.readouterr().out


def _fake_exchange_client(monkeypatch: pytest.MonkeyPatch) -> None:
    team = {"id": "T0123456789", "name": "Your Workspace", "domain": "your-workspace"}
    team |= {"token": "xoxc-test-token-1", "user_id": "U0ALICE0001"}
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json={"teams": {"T0123456789": team}}))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(cli_main.httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw))


def test_login_signs_in_through_sign_in_with_browser_and_saves(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls: list[dict[str, Any]] = []

    async def fake_sign_in(**kwargs: Any) -> SignInResult:
        calls.append(kwargs)
        for status in ("opening", "waiting", "verifying", "done"):
            kwargs["on_status"](status)
        return SignInResult(cookie_d="xoxd-test-cookie-value", workspaces=[])

    monkeypatch.setattr(cli_main, "sign_in_with_browser", fake_sign_in)
    monkeypatch.setenv("GABRIEL_CHROME_PATH", "/opt/chromium/chrome")
    _fake_exchange_client(monkeypatch)

    assert main(["auth", "login", "your-workspace"]) == 0

    assert calls[0]["workspace"] == "your-workspace"
    assert calls[0]["browser_path"] == "/opt/chromium/chrome"
    stored = load_session()
    assert stored is not None
    assert (stored.cookie_d, stored.workspace) == ("xoxd-test-cookie-value", "your-workspace")
    out = capsys.readouterr()
    assert "Opening a sign-in window" in out.err
    assert "your-workspace (T0123456789) user=U0ALICE0001" in out.out
    for text in (out.out, out.err):
        assert "xoxd-test-cookie-value" not in text
        assert "xoxc-test-token-1" not in text


def test_login_saves_nothing_when_sign_in_fails(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_sign_in(**kwargs: Any) -> SignInResult:
        assert kwargs["workspace"] is None
        raise GabrielError("SIGN_IN_TIMEOUT", "sign-in did not complete within 300 s")

    monkeypatch.setattr(cli_main, "sign_in_with_browser", fake_sign_in)
    monkeypatch.delenv("GABRIEL_CHROME_PATH", raising=False)
    assert main(["auth", "login"]) == 1
    assert load_session() is None
    assert "SIGN_IN_TIMEOUT" in capsys.readouterr().err
