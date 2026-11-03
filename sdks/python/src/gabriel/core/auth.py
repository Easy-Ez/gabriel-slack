"""Session credentials: cookie → token exchange and the auth state machine.

The ``d`` cookie is the root credential. The workspace token (``xoxc-…``) is derived from
it and refreshed transparently; a dead cookie is terminal (``AUTH_DEAD``).
"""

from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote, urljoin

import httpx

from ..errors import GabrielError
from ..util.aio import AbortSignal, Singleflight, race_signal, sleep
from ..util.logger import Logger, silent_logger
from ..util.redact import redact_secret
from .metadata import ClientMetadata, Endpoints

AuthState = Literal["UNINITIALIZED", "VALID", "REFRESHING", "AUTH_DEAD"]

RefreshTrigger = Literal["invalid_auth", "token_expired"]

_EXCHANGE_TIMEOUT = 15.0
_DOMAIN_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")


@dataclass(slots=True, kw_only=True)
class AuthTeam:
    """One workspace the session cookie is signed in to. ``token`` is sensitive."""

    id: str
    name: str
    domain: str
    url: str
    user_id: str
    token: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"AuthTeam(id={self.id!r}, name={self.name!r}, domain={self.domain!r}, url={self.url!r}, "
            f"user_id={self.user_id!r}, token={redact_secret(self.token)!r})"
        )


@dataclass(slots=True, kw_only=True)
class CookieExchangeResult:
    teams: list[AuthTeam]
    #: Login timestamp reported by the exchange endpoint.
    lc: str | None = None


@dataclass(slots=True, kw_only=True)
class WorkspaceSession:
    """Active workspace session. Never log or serialize this object (its repr is redacted)."""

    team_id: str
    domain: str
    #: Workspace API base, e.g. ``https://your-workspace.slack.com/``.
    api_base: str
    user_id: str
    token: str
    cookie_d: str
    team_name: str | None = None

    def __repr__(self) -> str:
        return (
            f"WorkspaceSession(team_id={self.team_id!r}, team_name={self.team_name!r}, domain={self.domain!r}, "
            f"api_base={self.api_base!r}, user_id={self.user_id!r}, token={redact_secret(self.token)!r}, "
            f"cookie_d={redact_secret(self.cookie_d)!r})"
        )


@dataclass(slots=True, kw_only=True)
class AuthRefreshed:
    team_id: str
    trigger: RefreshTrigger
    #: Epoch milliseconds.
    refreshed_at: int
    type: Literal["refreshed"] = "refreshed"


@dataclass(slots=True, kw_only=True)
class AuthDead:
    team_id: str
    reason: str
    type: Literal["dead"] = "dead"


AuthEvent = AuthRefreshed | AuthDead


@dataclass(slots=True, kw_only=True)
class AuthOptions:
    #: Required. Root session credential: the ``.slack.com`` ``d`` cookie (``xoxd-…``).
    cookie_d: str
    #: Optional workspace token (``xoxc-…``). Derived from ``cookie_d`` when omitted.
    token: str | None = None
    #: Workspace domain (``your-workspace`` / ``your-workspace.slack.com``) or team ID.
    workspace: str | None = None
    #: Allow-list of team IDs. Starting against any other workspace is refused.
    allowed_teams: list[str] | None = None

    def __repr__(self) -> str:
        token = redact_secret(self.token) if self.token else None
        return (
            f"AuthOptions(cookie_d={redact_secret(self.cookie_d)!r}, token={token!r}, "
            f"workspace={self.workspace!r}, allowed_teams={self.allowed_teams!r})"
        )


@dataclass(slots=True, kw_only=True)
class AuthDeps:
    http_client: httpx.AsyncClient
    metadata: ClientMetadata
    endpoints: Endpoints
    logger: Logger = silent_logger
    #: Wall clock in seconds (used for ``refreshed_at``).
    now: Callable[[], float] = time.time


def _base_headers(metadata: ClientMetadata) -> dict[str, str]:
    return {"User-Agent": metadata.user_agent, "Accept": "application/json"}


def normalize_cookie(cookie_d: str) -> str:
    # Cookies copied from DevTools are often URL-decoded; the wire format is encoded.
    trimmed = cookie_d.strip()
    if re.search(r"[+/=]", trimmed) and not re.search(r"%[0-9A-F]{2}", trimmed, re.IGNORECASE):
        # Same character set as encodeURIComponent.
        return quote(trimmed, safe="!~*'()")
    return trimmed


def _clean_workspace(value: str) -> str:
    out = value.strip().lower()
    out = re.sub(r"^https?://", "", out)
    out = re.sub(r"\.slack\.com/?$", "", out)
    return re.sub(r"/$", "", out)


async def exchange_cookie(deps: AuthDeps, cookie_d: str, signal: AbortSignal | None = None) -> CookieExchangeResult:
    """Exchanges the session cookie for every signed-in workspace's token and metadata.

    ``GET {auth_url}`` carrying only ``Cookie: d=…``. The result holds tokens — never log it.
    """
    try:
        request = deps.http_client.get(
            deps.endpoints.auth_url,
            headers={**_base_headers(deps.metadata), "Cookie": f"d={normalize_cookie(cookie_d)}"},
            follow_redirects=False,
            timeout=None if signal is not None else _EXCHANGE_TIMEOUT,
        )
        resp = await race_signal(request, signal)
    except Exception as err:
        raise GabrielError("NETWORK_TIMEOUT", "cookie exchange request failed", cause=err) from err
    status = resp.status_code
    if 300 <= status < 400:
        raise GabrielError(
            "AUTH_DEAD", f"cookie exchange redirected (HTTP {status}); the session cookie is not signed in"
        )
    if status in (401, 403):
        try:
            text = resp.text
        except Exception:
            text = ""
        # Slack rejects outdated browsers with 403 "browser is not supported": a client-identity
        # problem, not a dead session — never enter the terminal AUTH_DEAD state for it.
        if re.search(r"browser|not supported|unsupported", text, re.IGNORECASE):
            raise GabrielError(
                "INVALID_STATE",
                "Slack rejected the client User-Agent as outdated; set advanced metadata.user_agent to a current "
                "Chrome User-Agent",
            )
        raise GabrielError("AUTH_DEAD", f"cookie exchange rejected (HTTP {status})")
    if status == 429:
        try:
            retry_after = float(resp.headers.get("retry-after") or "5")
        except ValueError:
            retry_after = 5.0
        raise GabrielError("RATE_LIMITED", "cookie exchange rate limited", retry_after=retry_after)
    if not resp.is_success:
        raise GabrielError("SERVER_ERROR", f"cookie exchange failed (HTTP {status})")
    try:
        body: Any = resp.json()
    except ValueError as err:
        raise GabrielError(
            "AUTH_DEAD", "cookie exchange returned a non-JSON response (likely a sign-in page)", cause=err
        ) from err
    if not isinstance(body, dict):
        body = {}
    raw_teams = body.get("teams")
    teams: list[AuthTeam] = []
    if isinstance(raw_teams, dict):
        for key, raw in raw_teams.items():
            if not isinstance(raw, dict):
                continue
            token = raw.get("token")
            if not isinstance(token, str) or not token:
                continue

            def s(name: str, raw: dict[str, Any] = raw) -> str:
                v = raw.get(name)
                return v if isinstance(v, str) else ""

            teams.append(
                AuthTeam(
                    id=s("id") if isinstance(raw.get("id"), str) else str(key),
                    name=s("name"),
                    domain=s("domain"),
                    url=s("url"),
                    user_id=s("user_id"),
                    token=token,
                )
            )
    if not teams:
        raise GabrielError("AUTH_DEAD", "cookie exchange returned no signed-in workspaces")
    lc = body.get("lc")
    return CookieExchangeResult(teams=teams, lc=lc if isinstance(lc, str) else None)


async def find_team(deps: AuthDeps, domain: str) -> dict[str, Any]:
    """Probes a workspace's sign-in policy (e.g. whether SSO is required). Requires no credentials."""
    clean = re.sub(r"^https?://", "", domain.strip())
    clean = re.sub(r"\.slack\.com/?$", "", clean, flags=re.IGNORECASE)
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", clean, re.IGNORECASE):
        raise GabrielError("INVALID_ARGUMENT", 'domain: expected a workspace subdomain like "your-workspace"')
    try:
        resp = await deps.http_client.post(
            urljoin(deps.endpoints.public_api_base, "auth.findTeam"),
            headers=_base_headers(deps.metadata),
            data={"domain": clean},
            timeout=_EXCHANGE_TIMEOUT,
        )
    except Exception as err:
        raise GabrielError("NETWORK_TIMEOUT", "auth.findTeam request failed", cause=err) from err
    if not resp.is_success:
        raise GabrielError("SERVER_ERROR", f"auth.findTeam failed (HTTP {resp.status_code})")
    try:
        body = resp.json()
    except ValueError as err:
        raise GabrielError("SERVER_ERROR", "auth.findTeam returned a non-JSON body", cause=err) from err
    return body if isinstance(body, dict) else {}


def select_team(teams: list[AuthTeam], workspace: str | None = None) -> AuthTeam:
    if workspace:
        wanted = _clean_workspace(workspace)
        for t in teams:
            if t.id.lower() == wanted or t.domain.lower() == wanted or t.name.lower() == wanted:
                return t
        raise GabrielError(
            "INVALID_ARGUMENT",
            f'workspace "{workspace}" is not among the signed-in workspaces ({", ".join(t.domain for t in teams)})',
        )
    if len(teams) == 1:
        return teams[0]
    raise GabrielError(
        "INVALID_ARGUMENT",
        f"the session is signed in to {len(teams)} workspaces; pass `workspace` to choose one of: "
        + ", ".join(t.domain for t in teams),
    )


def _api_base_from_team(url: str, domain: str, override: str | None) -> str:
    if override:
        return override if override.endswith("/") else f"{override}/"
    if url:
        return url if url.endswith("/") else f"{url}/"
    return f"https://{domain}.slack.com/"


@dataclass(slots=True, kw_only=True)
class TokenIdentity:
    team_id: str
    user_id: str


#: Verifies an explicitly supplied token (``auth.test``) and returns the identity it belongs to.
VerifyToken = Callable[[WorkspaceSession], Awaitable[TokenIdentity]]


class AuthManager:
    """Credential state machine: ``VALID → REFRESHING → VALID | AUTH_DEAD``.

    Concurrent :meth:`refresh` calls collapse into one exchange (singleflight), and a dead
    root credential is terminal — no further retries are attempted.
    """

    def __init__(self, opts: AuthOptions, deps: AuthDeps) -> None:
        if not isinstance(opts.cookie_d, str) or len(opts.cookie_d.strip()) < 8:
            raise GabrielError("INVALID_ARGUMENT", "auth.cookie_d is required (the `d` session cookie)")
        self._opts = opts
        self._deps = deps
        self._session: WorkspaceSession | None = None
        self._state: AuthState = "UNINITIALIZED"
        self._flight: Singleflight[None] = Singleflight()
        self._listeners: list[Callable[[AuthEvent], None]] = []
        self._logger = deps.logger
        self._now = deps.now

    def __repr__(self) -> str:
        return f"AuthManager(state={self._state!r}, team_id={self._session.team_id if self._session else None!r})"

    @property
    def state(self) -> AuthState:
        return self._state

    def on_event(self, listener: Callable[[AuthEvent], None]) -> Callable[[], None]:
        self._listeners.append(listener)

        def remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return remove

    async def initialize(self, verify_token: VerifyToken | None = None) -> WorkspaceSession:
        """Resolves the workspace session (exchange or token verification)."""
        cookie_d = normalize_cookie(self._opts.cookie_d)
        if self._opts.token and self._opts.workspace and verify_token is not None:
            domain = _clean_workspace(self._opts.workspace)
            if not _DOMAIN_RE.fullmatch(domain):
                raise GabrielError(
                    "INVALID_ARGUMENT",
                    'auth.workspace must be a workspace subdomain like "your-workspace" when a token is supplied',
                )
            provisional = WorkspaceSession(
                team_id="",
                domain=domain,
                api_base=_api_base_from_team("", domain, self._deps.endpoints.workspace_api_base),
                user_id="",
                token=self._opts.token,
                cookie_d=cookie_d,
            )
            ident = await verify_token(provisional)
            provisional.team_id = ident.team_id
            provisional.user_id = ident.user_id
            session = provisional
        else:
            result = await exchange_cookie(self._deps, cookie_d)
            team = select_team(result.teams, self._opts.workspace)
            session = WorkspaceSession(
                team_id=team.id,
                team_name=team.name,
                domain=team.domain,
                api_base=_api_base_from_team(team.url, team.domain, self._deps.endpoints.workspace_api_base),
                user_id=team.user_id,
                token=team.token,
                cookie_d=cookie_d,
            )
        allowed = self._opts.allowed_teams
        if allowed and session.team_id not in allowed:
            raise GabrielError("PERMISSION_DENIED", f"team {session.team_id} is not in auth.allowed_teams")
        self._session = session
        self._state = "VALID"
        self._logger.info({"teamId": session.team_id, "token": redact_secret(session.token)}, "workspace session ready")
        return session

    def current(self) -> WorkspaceSession:
        if self._state == "AUTH_DEAD":
            raise GabrielError("AUTH_DEAD", "root session credential is no longer valid")
        if self._session is None:
            raise GabrielError("INVALID_STATE", "auth is not initialized; call client.start() first")
        return self._session

    async def refresh(self, trigger: RefreshTrigger, failed_token: str | None = None) -> None:
        """Re-derives the workspace token.

        ``failed_token`` lets late callers detect that a refresh already happened after their
        request failed, so they just replay.
        """
        if self._state == "AUTH_DEAD":
            raise GabrielError("AUTH_DEAD", "root session credential is no longer valid")
        if self._session is not None and failed_token is not None and self._session.token != failed_token:
            return
        await self._flight.do("refresh", lambda: self._do_refresh(trigger))

    async def _do_refresh(self, trigger: RefreshTrigger) -> None:
        session = self.current()
        self._state = "REFRESHING"
        last_err: BaseException | None = None
        for attempt in range(3):
            try:
                result = await exchange_cookie(self._deps, session.cookie_d)
                team = next((t for t in result.teams if t.id == session.team_id), None)
                if team is None:
                    self.mark_dead("team_signed_out")
                    raise GabrielError("AUTH_DEAD", f"the session is no longer signed in to team {session.team_id}")
                self._session = WorkspaceSession(
                    team_id=session.team_id,
                    team_name=session.team_name,
                    domain=session.domain,
                    api_base=session.api_base,
                    user_id=team.user_id or session.user_id,
                    token=team.token,
                    cookie_d=session.cookie_d,
                )
                self._state = "VALID"
                self._emit(
                    AuthRefreshed(team_id=session.team_id, trigger=trigger, refreshed_at=int(self._now() * 1000))
                )
                return
            except Exception as err:
                if isinstance(err, GabrielError) and err.code == "AUTH_DEAD":
                    self.mark_dead("cookie_invalid")
                    raise
                last_err = err
                await sleep(0.5 * 2**attempt)
        # Transient failure: keep the old token, a later request will retry the refresh.
        self._state = "VALID"
        if isinstance(last_err, GabrielError):
            raise last_err
        raise GabrielError("NETWORK_TIMEOUT", "token refresh failed", cause=last_err)

    def mark_dead(self, reason: str) -> None:
        """Enters the terminal AUTH_DEAD state."""
        if self._state == "AUTH_DEAD":
            return
        self._state = "AUTH_DEAD"
        team_id = self._session.team_id if self._session else None
        self._logger.error({"teamId": team_id, "reason": reason}, "root session credential is dead")
        self._emit(AuthDead(team_id=team_id or "", reason=reason))

    def _emit(self, evt: AuthEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(evt)
            except Exception as err:
                self._logger.error({"err": str(err)}, "auth listener threw")
