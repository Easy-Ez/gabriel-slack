"""Local session cookie storage used by the ``gabriel`` CLI.

``{config_dir}/auth.json``, directory 0700, file 0600 — the same file and format as the Node
CLI, so either one can read a session the other saved. Prefer an OS keychain or secret manager
in production; this file exists for local development convenience.
"""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote

from ..errors import GabrielError


@dataclass(kw_only=True, slots=True)
class StoredSession:
    cookie_d: str
    saved_at: str = ""
    workspace: str | None = None

    def __repr__(self) -> str:
        from ..util.redact import redact_secret

        return f"StoredSession(cookie_d={redact_secret(self.cookie_d)!r}, workspace={self.workspace!r})"


@dataclass(kw_only=True, slots=True)
class SessionCookie:
    """Credentials for :meth:`GabrielClient.from_cookie` (``**asdict``-friendly)."""

    cookie_d: str
    workspace: str | None = None

    def __repr__(self) -> str:
        from ..util.redact import redact_secret

        return f"SessionCookie(cookie_d={redact_secret(self.cookie_d)!r}, workspace={self.workspace!r})"


def config_dir() -> Path:
    explicit = os.environ.get("GABRIEL_CONFIG_DIR")
    if explicit:
        return Path(explicit).resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return (Path(xdg).resolve() if xdg else Path.home() / ".config") / "gabriel"


def session_file() -> Path:
    return config_dir() / "auth.json"


def save_session(cookie_d: str, workspace: str | None = None) -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(d, 0o700)
    file = session_file()
    tmp = file.with_name(f"{file.name}.{os.getpid()}.tmp")
    body: dict[str, str] = {"cookieD": cookie_d}
    if workspace:
        body["workspace"] = workspace
    body["savedAt"] = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(body, fh, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, file)
    return file


def load_session() -> StoredSession | None:
    file = session_file()
    if not file.exists():
        return None
    if sys.platform != "win32":
        mode = stat.S_IMODE(file.stat().st_mode)
        if mode & 0o077:
            raise GabrielError(
                "PERMISSION_DENIED",
                f'{file} is accessible by other users (mode {mode:o}); run: chmod 600 "{file}"',
            )
    parsed = json.loads(file.read_text("utf-8"))
    cookie = parsed.get("cookieD") if isinstance(parsed, dict) else None
    if not isinstance(cookie, str) or len(cookie) < 8:
        return None
    workspace = parsed.get("workspace")
    return StoredSession(
        cookie_d=cookie,
        saved_at=parsed.get("savedAt") or "",
        workspace=workspace if isinstance(workspace, str) and workspace else None,
    )


def delete_session() -> bool:
    file = session_file()
    if not file.exists():
        return False
    file.unlink()
    return True


def load_session_cookie() -> SessionCookie | None:
    """``GABRIEL_COOKIE_D`` from the environment, else the CLI-saved session."""
    env = (os.environ.get("GABRIEL_COOKIE_D") or "").strip()
    if env:
        return SessionCookie(cookie_d=env, workspace=os.environ.get("GABRIEL_WORKSPACE") or None)
    stored = load_session()
    return SessionCookie(cookie_d=stored.cookie_d, workspace=stored.workspace) if stored else None


_HEADER = re.compile(r"""(?:-H|--header)\s+(['"])\s*cookie:\s*([\s\S]*?)\1""", re.IGNORECASE)
_COOKIE_FLAG = re.compile(r"""(?:-b|--cookie)\s+(['"])([\s\S]*?)\1""")


def parse_curl_for_cookie(curl: str) -> str | None:
    """Extracts the ``d`` cookie from a browser "Copy as cURL" command."""
    candidates = [m.group(2) for m in _HEADER.finditer(curl)] + [m.group(2) for m in _COOKIE_FLAG.finditer(curl)]
    for c in candidates:
        for part in c.split(";"):
            k, sep, rest = part.strip().partition("=")
            if k == "d" and sep:
                value = rest.strip()
                if unquote(value).startswith("xoxd-"):
                    return value
    return None
