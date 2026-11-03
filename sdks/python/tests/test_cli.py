"""CLI credential helpers (port of sdks/node/test/cli.test.ts)."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from gabriel.cli.main import main
from gabriel.cli.session_store import (
    delete_session,
    load_session,
    load_session_cookie,
    parse_curl_for_cookie,
    save_session,
)
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
