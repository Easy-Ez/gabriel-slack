"""Client identity metadata and network endpoints.

The Slack Web client protocol rejects outdated browser User-Agents ("browser is not
supported"), so all HTTP and WebSocket traffic carries one centrally managed, overridable
identity.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode


@dataclass(frozen=True, slots=True)
class ClientMetadata:
    #: Browser-compatible User-Agent sent on every request.
    user_agent: str
    #: Web client build timestamp, sent as ``agent_version`` / ``_x_version_ts``.
    agent_version: str
    #: ``Origin`` header for API and WebSocket requests.
    origin: str


DEFAULT_CLIENT_METADATA = ClientMetadata(
    user_agent=(
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/148.0.0.0 Safari/537.36"
    ),
    agent_version="1792732050",
    origin="https://app.slack.com",
)


@dataclass(frozen=True, slots=True)
class Endpoints:
    """Network endpoints. Overridable for tests, proxies and regional deployments."""

    #: Cookie → token exchange endpoint.
    auth_url: str = "https://app.slack.com/auth?app=client&response_type=json"
    #: Unauthenticated API base used by ``auth.findTeam``.
    public_api_base: str = "https://slack.com/api/"
    #: EdgeAPI base; ``{team_id}/{endpoint}`` is appended.
    edge_api_base: str = "https://edgeapi.slack.com/cache/"
    #: WebSocket gateway base URL.
    ws_base: str = "wss://wss-primary.slack.com/"
    #: Optional override for the workspace API base (normally derived from the team URL,
    #: e.g. ``https://your-workspace.slack.com/``).
    workspace_api_base: str | None = None


DEFAULT_ENDPOINTS = Endpoints()


def build_start_args(agent_version: str) -> str:
    """Decoded ``start_args`` used by the web client handshake."""
    params = {
        "agent": "client",
        "org_wide_aware": "true",
        "agent_version": agent_version,
        "eac_cache_ts": "true",
        "cache_ts": "0",
        "name_tagging": "true",
        "only_self_subteams": "true",
        "connect_only": "true",
        "ms_latest": "true",
    }
    return f"?{urlencode(params)}"
