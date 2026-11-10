"""Shared HTTP transport for Web API and EdgeAPI calls (spec/behavior.md §4)."""

from __future__ import annotations

import asyncio
import json
import math
import re
import secrets
import time
from collections.abc import AsyncIterable, Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urljoin, urlsplit

import httpx

from ..errors import TOKEN_INVALID_ERRORS, GabrielError, cancelled_error, from_slack_error
from ..util.aio import AbortSignal, full_jitter_delay, race_signal, sleep
from ..util.logger import Logger, silent_logger
from .auth import AuthManager, WorkspaceSession
from .metadata import ClientMetadata, Endpoints
from .types import MetricsSink, RequestOpts, noop_metrics

DEFAULT_WEB_API_TIMEOUT = 15.0
DEFAULT_EDGE_API_TIMEOUT = 10.0

_RETRY_BASE = 0.5
_RETRY_CAP = 8.0

READ_ONLY_PREFIXES: tuple[str, ...] = (
    "conversations.history",
    "conversations.replies",
    "conversations.info",
    "client.",
    "messages.list",
    "activity.feed",
    "users.",
    "usergroups.list",
    "usergroups.users.list",
    "dnd.",
    "search.",
    "files.info",
    "auth.test",
    "team.",
    "emoji.",
    "bots.info",
)

#: Overwrite-style or naturally idempotent writes that are safe to replay.
IDEMPOTENT_WRITES: frozenset[str] = frozenset(
    {
        "chat.update",
        "reactions.add",
        "reactions.remove",
        "conversations.mark",
        "subscriptions.thread.mark",
        "subscriptions.thread.add",
        "subscriptions.thread.remove",
    }
)


def is_safe_to_retry(method: str, fields: Mapping[str, Any]) -> bool:
    if method.startswith(READ_ONLY_PREFIXES):
        return True
    if method in IDEMPOTENT_WRITES:
        return True
    # chat.postMessage is deduplicated server-side by client_msg_id.
    return method == "chat.postMessage" and isinstance(fields.get("client_msg_id"), str)


def to_json(value: Any) -> str:
    """``JSON.stringify`` equivalent (compact separators, non-ASCII kept)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _js_number(value: int | float) -> str:
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        if value.is_integer() and abs(value) < 1e21:
            return str(int(value))
        return repr(value)
    return str(value)


def encode_field(value: Any) -> str | None:
    """Encodes one form field the way the web client does: objects become JSON."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return _js_number(value)
    return to_json(value)


_CRLF_RE = re.compile(r"\r?\n|\r(?!\n)")


def _normalize_linefeeds(value: str) -> str:
    return _CRLF_RE.sub("\r\n", value)


def _escape_name(name: str) -> str:
    return name.replace("\n", "%0A").replace("\r", "%0D").replace('"', "%22")


def encode_multipart(fields: Mapping[str, str], boundary: str | None = None) -> tuple[bytes, str]:
    """Serializes string fields as ``multipart/form-data`` exactly like Node's ``FormData``.

    Line breaks are normalized to CRLF (spec §4: length budgets count ``\\n`` as 2).
    Returns ``(body, content_type)``.
    """
    if boundary is None:
        boundary = f"----formdata-undici-0{secrets.randbelow(10**11):011d}"
    prefix = f"--{boundary}\r\nContent-Disposition: form-data"
    out: list[str] = []
    for name, value in fields.items():
        out.append(
            f'{prefix}; name="{_escape_name(_normalize_linefeeds(name))}"\r\n\r\n{_normalize_linefeeds(value)}\r\n'
        )
    out.append(f"--{boundary}--\r\n")
    return "".join(out).encode("utf-8"), f"multipart/form-data; boundary={boundary}"


_SLACK_HOST = re.compile(r"(^|\.)slack(-edge|-files)?\.com$", re.IGNORECASE)
_DEFAULT_PORTS = {"http": 80, "https": 443, "ws": 80, "wss": 443}


def _host(url: str) -> str:
    """``URL.host`` equivalent: hostname plus a non-default port."""
    parts = urlsplit(url)
    hostname = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is not None and port != _DEFAULT_PORTS.get(parts.scheme.lower()):
        return f"{hostname}:{port}"
    return hostname


def is_slack_host(url: str) -> bool:
    parts = urlsplit(url)
    return parts.scheme.lower() == "https" and _SLACK_HOST.search(parts.hostname or "") is not None


def is_trusted_host(url: str, endpoints: Endpoints) -> bool:
    """Credentials go only to https Slack hosts, or to explicitly configured endpoint hosts (tests, proxies)."""
    parts = urlsplit(url)
    configured = [_host(e) for e in (endpoints.edge_api_base, endpoints.workspace_api_base, endpoints.auth_url) if e]
    if _host(url) in configured:
        return parts.scheme.lower() == "https" or parts.hostname in ("localhost", "127.0.0.1")
    return is_slack_host(url)


def _x_id() -> str:
    return f"{secrets.token_hex(4)}-{time.time():.3f}"


@dataclass(slots=True)
class _Built:
    url: str
    headers: dict[str, str]
    content: bytes


class HttpEngine:
    """Shared HTTP transport for Web API and EdgeAPI calls.

    - default timeouts (Web API 15 s, EdgeAPI 10 s);
    - one 429 cool-down bucket per method, shared by every caller;
    - retries by idempotency class (reads, ``client_msg_id`` writes, overwrite writes);
    - transparent token refresh + single replay on ``invalid_auth``.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        auth: AuthManager,
        metadata: ClientMetadata,
        endpoints: Endpoints,
        logger: Logger = silent_logger,
        metrics: MetricsSink = noop_metrics,
        max_retries: int = 3,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = http_client
        self._auth = auth
        self._metadata = metadata
        self._endpoints = endpoints
        self._logger = logger
        self._metrics = metrics
        self._max_retries = max_retries
        self._now = now
        self._cooldown_until: dict[str, float] = {}

    async def call_web_api(
        self, method: str, fields: Mapping[str, Any] | None = None, opts: RequestOpts | None = None
    ) -> dict[str, Any]:
        """Calls ``POST {workspace}/api/{method}`` with the web client's multipart form encoding."""
        if not isinstance(method, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_.]{1,100}", method):
            raise GabrielError("INVALID_ARGUMENT", "invalid Web API method name")
        fields = dict(fields or {})
        opts = opts or RequestOpts()
        if opts.retry == "none":
            safe = False
        elif opts.retry == "safe":
            safe = True
        else:
            safe = is_safe_to_retry(method, fields)

        def build(session: WorkspaceSession) -> _Built:
            query = urlencode(
                {
                    "_x_id": _x_id(),
                    "slack_route": session.team_id,
                    "_x_version_ts": self._metadata.agent_version,
                    "_x_gantry": "true",
                }
            )
            url = f"{urljoin(session.api_base, f'api/{method}')}?{query}"
            # Insertion order mirrors FormData.set(): re-setting a key keeps its first position.
            form: dict[str, str] = {"token": session.token}
            for k, v in fields.items():
                encoded = encode_field(v)
                if encoded is not None:
                    form[k] = encoded
            form["_x_reason"] = opts.reason if opts.reason is not None else "gabriel-sdk"
            form["_x_mode"] = "online"
            form["_x_sonic"] = "true"
            form["_x_app_name"] = "client"
            body, content_type = encode_multipart(form)
            return _Built(url, {**self._headers(session), "Content-Type": content_type}, body)

        return await self._execute(f"web:{method}", method, safe, opts, DEFAULT_WEB_API_TIMEOUT, build)

    async def call_edge_api(
        self, endpoint: str, payload: Mapping[str, Any] | None = None, opts: RequestOpts | None = None
    ) -> dict[str, Any]:
        """Calls ``POST {edge_api_base}{team_id}/{endpoint}`` with a JSON body."""
        if not isinstance(endpoint, str) or not re.fullmatch(r"[a-zA-Z]+/[a-zA-Z]+", endpoint):
            raise GabrielError("INVALID_ARGUMENT", 'invalid EdgeAPI endpoint (expected e.g. "users/info")')
        payload = dict(payload or {})
        opts = opts or RequestOpts()
        safe = opts.retry != "none"

        def build(session: WorkspaceSession) -> _Built:
            query = urlencode({"_x_app_name": "client", "_x_num_retries": "0"})
            url = f"{urljoin(self._endpoints.edge_api_base, f'{session.team_id}/{endpoint}')}?{query}"
            body = to_json({"token": session.token, **payload}).encode("utf-8")
            return _Built(url, {**self._headers(session), "Content-Type": "application/json"}, body)

        return await self._execute(f"edge:{endpoint}", endpoint, safe, opts, DEFAULT_EDGE_API_TIMEOUT, build)

    async def fetch_authenticated(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: Mapping[str, str] | None = None,
        content: bytes | AsyncIterable[bytes] | None = None,
        signal: AbortSignal | None = None,
        timeout: float | None = None,
        bearer: bool = True,
        stream: bool = False,
    ) -> httpx.Response:
        """Authenticated raw request (file transfer).

        Adds cookie + bearer token only for Slack-owned hosts; never sends credentials to
        arbitrary URLs. Redirects are followed (httpx drops the cookie and, cross-origin, the
        bearer token on redirect). ``timeout`` is a total budget in seconds (``None``: none).
        With ``stream=True`` the body is not read; the caller must ``await resp.aclose()``.
        """
        if not is_slack_host(url):
            raise GabrielError(
                "INVALID_ARGUMENT", f"refusing to send session credentials to non-Slack host {urlsplit(url).hostname}"
            )
        session = self._auth.current()
        merged = {**self._headers(session), **(headers or {})}
        if bearer:
            merged["Authorization"] = f"Bearer {session.token}"
        request = self._client.build_request(method, url, headers=merged, content=content, timeout=timeout)

        async def send() -> httpx.Response:
            async with asyncio.timeout(timeout):
                return await self._client.send(request, stream=stream, follow_redirects=True)

        try:
            return await race_signal(send(), signal)
        except GabrielError:
            raise
        except Exception as err:
            if signal is not None and signal.aborted:
                raise cancelled_error(signal.reason) from err
            raise GabrielError("NETWORK_TIMEOUT", f"request to {urlsplit(url).hostname} failed", cause=err) from err

    def _headers(self, session: WorkspaceSession) -> dict[str, str]:
        return {
            "Cookie": f"d={session.cookie_d}",
            "Origin": self._metadata.origin,
            "User-Agent": self._metadata.user_agent,
        }

    async def _send(self, built: _Built, budget: float) -> httpx.Response:
        async with asyncio.timeout(budget):
            resp = await self._client.post(built.url, headers=built.headers, content=built.content, timeout=budget)
            await resp.aread()
            return resp

    async def _execute(
        self,
        bucket: str,
        label: str,
        safe: bool,
        opts: RequestOpts,
        default_timeout: float,
        build: Callable[[WorkspaceSession], _Built],
    ) -> dict[str, Any]:
        signal = opts.signal
        attempt = 0
        auth_replayed = False
        while True:
            if signal is not None and signal.aborted:
                raise cancelled_error(signal.reason)
            await self._wait_cooldown(bucket, signal)
            session = self._auth.current()
            built = build(session)
            if not is_trusted_host(built.url, self._endpoints):
                raise GabrielError("INVALID_ARGUMENT", f"refusing to send session credentials to {_host(built.url)}")
            started = self._now()
            timeout = opts.timeout if opts.timeout is not None else default_timeout
            try:
                resp = await race_signal(self._send(built, timeout), signal)
            except Exception as err:
                if signal is not None and signal.aborted:
                    raise cancelled_error(signal.reason) from err
                timed_out = isinstance(err, TimeoutError | httpx.TimeoutException)
                error = GabrielError(
                    "NETWORK_TIMEOUT", f"{label}: {'request timed out' if timed_out else 'network error'}", cause=err
                )
                if safe and attempt < self._max_retries:
                    await sleep(full_jitter_delay(attempt, _RETRY_BASE, _RETRY_CAP), signal)
                    attempt += 1
                    continue
                raise error from err
            finally:
                self._metrics.observe("api_request_latency_ms", (self._now() - started) * 1000, {"method": label})

            if resp.status_code == 429:
                try:
                    header = float(resp.headers.get("retry-after") or "5")
                except ValueError:
                    header = 5.0
                retry_after = max(1.0, header)
                self._cooldown_until[bucket] = self._now() + retry_after
                self._metrics.increment("rate_limited_429_total", {"method": label})
                self._logger.warn({"method": label, "retryAfterMs": int(retry_after * 1000)}, "rate limited")
                if attempt < self._max_retries:
                    attempt += 1
                    continue
                raise GabrielError(
                    "RATE_LIMITED", f"{label}: rate limited", retry_after=retry_after, raw_slack_error="ratelimited"
                )
            if resp.status_code >= 500:
                if safe and attempt < self._max_retries:
                    await sleep(full_jitter_delay(attempt, _RETRY_BASE, _RETRY_CAP), signal)
                    attempt += 1
                    continue
                raise GabrielError("SERVER_ERROR", f"{label}: HTTP {resp.status_code}")

            try:
                body: Any = resp.json()
            except ValueError as err:
                raise GabrielError(
                    "SERVER_ERROR", f"{label}: HTTP {resp.status_code} with a non-JSON body", cause=err
                ) from err
            if not isinstance(body, dict):
                raise GabrielError("SERVER_ERROR", f"{label}: HTTP {resp.status_code} with a non-object JSON body")
            if body.get("ok") is False:
                raw_error = body.get("error")
                slack_error = raw_error if isinstance(raw_error, str) else "unknown_error"
                if slack_error in TOKEN_INVALID_ERRORS and not auth_replayed:
                    auth_replayed = True
                    await self._auth.refresh(
                        "token_expired" if slack_error == "token_expired" else "invalid_auth", session.token
                    )
                    continue
                if slack_error in ("ratelimited", "rate_limited") and attempt < self._max_retries:
                    self._cooldown_until[bucket] = self._now() + 5.0
                    attempt += 1
                    continue
                mapped = from_slack_error(label, slack_error)
                if mapped.retryable and mapped.code != "TOKEN_EXPIRED" and safe and attempt < self._max_retries:
                    await sleep(full_jitter_delay(attempt, _RETRY_BASE, _RETRY_CAP), signal)
                    attempt += 1
                    continue
                raise mapped
            if not resp.is_success:
                raise GabrielError("SLACK_API_ERROR", f"{label}: HTTP {resp.status_code}")
            return body

    async def _wait_cooldown(self, bucket: str, signal: AbortSignal | None) -> None:
        until = self._cooldown_until.get(bucket)
        if until is None:
            return
        wait = until - self._now()
        if wait <= 0:
            self._cooldown_until.pop(bucket, None)
            return
        await sleep(wait, signal)
