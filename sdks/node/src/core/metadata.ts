/**
 * Client identity metadata. The Slack Web client protocol rejects outdated
 * browser User-Agents ("browser is not supported"), so all HTTP and WebSocket
 * traffic carries one centrally managed, overridable identity.
 */
export interface ClientMetadata {
  /** Browser-compatible User-Agent sent on every request. */
  userAgent: string;
  /** Web client build timestamp, sent as `agent_version` / `_x_version_ts`. */
  agentVersion: string;
  /** `Origin` header for API and WebSocket requests. */
  origin: string;
}

export const DEFAULT_CLIENT_METADATA: Readonly<ClientMetadata> = Object.freeze({
  userAgent:
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
  agentVersion: "1792732050",
  origin: "https://app.slack.com",
});

/** Network endpoints. Overridable for tests, proxies and regional deployments. */
export interface Endpoints {
  /** Cookie → token exchange endpoint. */
  authUrl: string;
  /** Unauthenticated API base used by `auth.findTeam`. */
  publicApiBase: string;
  /** EdgeAPI base; `{teamId}/{endpoint}` is appended. */
  edgeApiBase: string;
  /** WebSocket gateway base URL. */
  wsBase: string;
  /**
   * Optional override for the workspace API base (normally derived from the team URL,
   * e.g. `https://your-workspace.slack.com/`).
   */
  workspaceApiBase?: string;
}

export const DEFAULT_ENDPOINTS: Readonly<Endpoints> = Object.freeze({
  authUrl: "https://app.slack.com/auth?app=client&response_type=json",
  publicApiBase: "https://slack.com/api/",
  edgeApiBase: "https://edgeapi.slack.com/cache/",
  wsBase: "wss://wss-primary.slack.com/",
});

/** Decoded `start_args` used by the web client handshake. */
export function buildStartArgs(agentVersion: string): string {
  const params = new URLSearchParams({
    agent: "client",
    org_wide_aware: "true",
    agent_version: agentVersion,
    eac_cache_ts: "true",
    cache_ts: "0",
    name_tagging: "true",
    only_self_subteams: "true",
    connect_only: "true",
    ms_latest: "true",
  });
  return `?${params.toString()}`;
}
