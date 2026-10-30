import { GabrielError } from "../errors.js";
import { Singleflight, sleep } from "../util/async.js";
import type { Logger } from "../util/logger.js";
import { silentLogger } from "../util/logger.js";
import { redactSecret } from "../util/redact.js";
import type { ClientMetadata, Endpoints } from "./metadata.js";
import type { FetchLike } from "./types.js";

/** One workspace the session cookie is signed in to. `token` is sensitive. */
export interface AuthTeam {
  id: string;
  name: string;
  domain: string;
  url: string;
  userId: string;
  token: string;
}

export interface CookieExchangeResult {
  teams: AuthTeam[];
  /** Login timestamp reported by the exchange endpoint. */
  lc?: string;
}

/** Active workspace session. Never log or serialize this object. */
export interface WorkspaceSession {
  teamId: string;
  teamName?: string;
  domain: string;
  /** Workspace API base, e.g. `https://your-workspace.slack.com/`. */
  apiBase: string;
  userId: string;
  token: string;
  cookieD: string;
}

export type AuthState = "UNINITIALIZED" | "VALID" | "REFRESHING" | "AUTH_DEAD";

export type RefreshTrigger = "invalid_auth" | "token_expired";

export type AuthEvent =
  | { type: "refreshed"; teamId: string; trigger: RefreshTrigger; refreshedAt: number }
  | { type: "dead"; teamId: string; reason: string };

export interface AuthOptions {
  /** Required. Root session credential: the `.slack.com` `d` cookie (`xoxd-…`). */
  cookieD: string;
  /** Optional workspace token (`xoxc-…`). Derived from `cookieD` when omitted. */
  token?: string;
  /** Workspace domain (`your-workspace` / `your-workspace.slack.com`) or team ID. */
  workspace?: string;
  /** Allow-list of team IDs. Starting against any other workspace is refused. */
  allowedTeams?: string[];
}

interface AuthDeps {
  fetch: FetchLike;
  metadata: ClientMetadata;
  endpoints: Endpoints;
  logger?: Logger;
  now?: () => number;
}

function baseHeaders(metadata: ClientMetadata): Record<string, string> {
  return { "User-Agent": metadata.userAgent, Accept: "application/json" };
}

function normalizeCookie(cookieD: string): string {
  // Cookies copied from DevTools are often URL-decoded; the wire format is encoded.
  const trimmed = cookieD.trim();
  return /[+/=]/.test(trimmed) && !/%[0-9A-F]{2}/i.test(trimmed) ? encodeURIComponent(trimmed) : trimmed;
}

/**
 * Exchanges the session cookie for every signed-in workspace's token and metadata
 * (`GET {authUrl}` carrying only `Cookie: d=…`).
 */
export async function exchangeCookie(deps: AuthDeps, cookieD: string, signal?: AbortSignal): Promise<CookieExchangeResult> {
  let resp: Response;
  try {
    resp = await deps.fetch(deps.endpoints.authUrl, {
      headers: { ...baseHeaders(deps.metadata), Cookie: `d=${normalizeCookie(cookieD)}` },
      redirect: "manual",
      signal: signal ?? AbortSignal.timeout(15_000),
    });
  } catch (err) {
    throw new GabrielError("NETWORK_TIMEOUT", "cookie exchange request failed", { cause: err });
  }
  if (resp.status >= 300 && resp.status < 400) {
    throw new GabrielError("AUTH_DEAD", `cookie exchange redirected (HTTP ${resp.status}); the session cookie is not signed in`);
  }
  if (resp.status === 401 || resp.status === 403) {
    const body = await resp.text().catch(() => "");
    // Slack rejects outdated browsers with 403 "browser is not supported": a client-identity
    // problem, not a dead session — never enter the terminal AUTH_DEAD state for it.
    if (/browser|not supported|unsupported/i.test(body)) {
      throw new GabrielError(
        "INVALID_STATE",
        "Slack rejected the client User-Agent as outdated; set advanced.metadata.userAgent to a current Chrome User-Agent",
      );
    }
    throw new GabrielError("AUTH_DEAD", `cookie exchange rejected (HTTP ${resp.status})`);
  }
  if (resp.status === 429) {
    const retryAfter = Number(resp.headers.get("retry-after") ?? "5");
    throw new GabrielError("RATE_LIMITED", "cookie exchange rate limited", { retryAfterMs: retryAfter * 1000 });
  }
  if (!resp.ok) {
    throw new GabrielError("SERVER_ERROR", `cookie exchange failed (HTTP ${resp.status})`);
  }
  let body: { teams?: Record<string, Record<string, unknown>>; lc?: string };
  try {
    body = (await resp.json()) as typeof body;
  } catch (err) {
    throw new GabrielError("AUTH_DEAD", "cookie exchange returned a non-JSON response (likely a sign-in page)", { cause: err });
  }
  const teams: AuthTeam[] = [];
  for (const [key, raw] of Object.entries(body.teams ?? {})) {
    const token = typeof raw.token === "string" ? raw.token : "";
    if (!token) continue;
    teams.push({
      id: typeof raw.id === "string" ? raw.id : key,
      name: typeof raw.name === "string" ? raw.name : "",
      domain: typeof raw.domain === "string" ? raw.domain : "",
      url: typeof raw.url === "string" ? raw.url : "",
      userId: typeof raw.user_id === "string" ? raw.user_id : "",
      token,
    });
  }
  if (teams.length === 0) {
    throw new GabrielError("AUTH_DEAD", "cookie exchange returned no signed-in workspaces");
  }
  return body.lc === undefined ? { teams } : { teams, lc: body.lc };
}

/** Probes a workspace's sign-in policy (e.g. whether SSO is required). Requires no credentials. */
export async function findTeam(deps: AuthDeps, domain: string): Promise<Record<string, unknown>> {
  const clean = domain.trim().replace(/^https?:\/\//, "").replace(/\.slack\.com\/?$/i, "");
  if (!/^[a-z0-9][a-z0-9-]{0,62}$/i.test(clean)) {
    throw new GabrielError("INVALID_ARGUMENT", "domain: expected a workspace subdomain like \"your-workspace\"");
  }
  const resp = await deps.fetch(new URL("auth.findTeam", deps.endpoints.publicApiBase), {
    method: "POST",
    headers: { ...baseHeaders(deps.metadata), "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ domain: clean }),
    signal: AbortSignal.timeout(15_000),
  });
  if (!resp.ok) throw new GabrielError("SERVER_ERROR", `auth.findTeam failed (HTTP ${resp.status})`);
  return (await resp.json()) as Record<string, unknown>;
}

export function selectTeam(teams: AuthTeam[], workspace?: string): AuthTeam {
  if (workspace) {
    const wanted = workspace.trim().toLowerCase().replace(/^https?:\/\//, "").replace(/\.slack\.com\/?$/, "").replace(/\/$/, "");
    const match = teams.find(
      (t) => t.id.toLowerCase() === wanted || t.domain.toLowerCase() === wanted || t.name.toLowerCase() === wanted,
    );
    if (!match) {
      throw new GabrielError(
        "INVALID_ARGUMENT",
        `workspace "${workspace}" is not among the signed-in workspaces (${teams.map((t) => t.domain).join(", ")})`,
      );
    }
    return match;
  }
  if (teams.length === 1) return teams[0]!;
  throw new GabrielError(
    "INVALID_ARGUMENT",
    `the session is signed in to ${teams.length} workspaces; pass \`workspace\` to choose one of: ${teams.map((t) => t.domain).join(", ")}`,
  );
}

function apiBaseFromTeam(team: { url: string; domain: string }, override?: string): string {
  if (override) return override.endsWith("/") ? override : `${override}/`;
  if (team.url) return team.url.endsWith("/") ? team.url : `${team.url}/`;
  return `https://${team.domain}.slack.com/`;
}

/**
 * Credential state machine: VALID → REFRESHING → VALID | AUTH_DEAD.
 * Concurrent `refresh()` calls collapse into one exchange (singleflight), and a
 * dead root credential is terminal — no further retries are attempted.
 */
export class AuthManager {
  private session: WorkspaceSession | undefined;
  private stateValue: AuthState = "UNINITIALIZED";
  private readonly flight = new Singleflight<void>();
  private readonly listeners = new Set<(evt: AuthEvent) => void>();
  private readonly logger: Logger;
  private readonly now: () => number;

  constructor(
    private readonly opts: AuthOptions,
    private readonly deps: AuthDeps,
  ) {
    if (typeof opts.cookieD !== "string" || opts.cookieD.trim().length < 8) {
      throw new GabrielError("INVALID_ARGUMENT", "auth.cookieD is required (the `d` session cookie)");
    }
    this.logger = deps.logger ?? silentLogger;
    this.now = deps.now ?? Date.now;
  }

  get state(): AuthState {
    return this.stateValue;
  }

  onEvent(listener: (evt: AuthEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Resolves the workspace session (exchange or token verification). */
  async initialize(verifyToken?: (session: WorkspaceSession) => Promise<{ teamId: string; userId: string }>): Promise<WorkspaceSession> {
    const cookieD = normalizeCookie(this.opts.cookieD);
    let session: WorkspaceSession;
    if (this.opts.token && this.opts.workspace && verifyToken) {
      const domain = this.opts.workspace.trim().toLowerCase().replace(/^https?:\/\//, "").replace(/\.slack\.com\/?$/, "").replace(/\/$/, "");
      if (!/^[a-z0-9][a-z0-9-]{0,62}$/.test(domain)) {
        throw new GabrielError("INVALID_ARGUMENT", "auth.workspace must be a workspace subdomain like \"your-workspace\" when a token is supplied");
      }
      const provisional: WorkspaceSession = {
        teamId: "",
        domain,
        apiBase: apiBaseFromTeam({ url: "", domain }, this.deps.endpoints.workspaceApiBase),
        userId: "",
        token: this.opts.token,
        cookieD,
      };
      const ident = await verifyToken(provisional);
      session = { ...provisional, teamId: ident.teamId, userId: ident.userId };
    } else {
      const { teams } = await exchangeCookie(this.deps, cookieD);
      const team = selectTeam(teams, this.opts.workspace);
      session = {
        teamId: team.id,
        teamName: team.name,
        domain: team.domain,
        apiBase: apiBaseFromTeam(team, this.deps.endpoints.workspaceApiBase),
        userId: team.userId,
        token: team.token,
        cookieD,
      };
    }
    if (this.opts.allowedTeams && this.opts.allowedTeams.length > 0 && !this.opts.allowedTeams.includes(session.teamId)) {
      throw new GabrielError("PERMISSION_DENIED", `team ${session.teamId} is not in auth.allowedTeams`);
    }
    this.session = session;
    this.stateValue = "VALID";
    this.logger.info({ teamId: session.teamId, token: redactSecret(session.token) }, "workspace session ready");
    return session;
  }

  current(): WorkspaceSession {
    if (this.stateValue === "AUTH_DEAD") throw new GabrielError("AUTH_DEAD", "root session credential is no longer valid");
    if (!this.session) throw new GabrielError("INVALID_STATE", "auth is not initialized; call client.start() first");
    return this.session;
  }

  /**
   * Re-derives the workspace token. `failedToken` lets late callers detect that a
   * refresh already happened after their request failed, so they just replay.
   */
  refresh(trigger: RefreshTrigger, failedToken?: string): Promise<void> {
    if (this.stateValue === "AUTH_DEAD") return Promise.reject(new GabrielError("AUTH_DEAD", "root session credential is no longer valid"));
    if (this.session && failedToken !== undefined && this.session.token !== failedToken) return Promise.resolve();
    return this.flight.do("refresh", () => this.doRefresh(trigger));
  }

  private async doRefresh(trigger: RefreshTrigger): Promise<void> {
    const session = this.current();
    this.stateValue = "REFRESHING";
    let lastErr: unknown;
    for (let attempt = 0; attempt < 3; attempt++) {
      try {
        const { teams } = await exchangeCookie(this.deps, session.cookieD);
        const team = teams.find((t) => t.id === session.teamId);
        if (!team) {
          this.markDead("team_signed_out");
          throw new GabrielError("AUTH_DEAD", `the session is no longer signed in to team ${session.teamId}`);
        }
        this.session = { ...session, token: team.token, userId: team.userId || session.userId };
        this.stateValue = "VALID";
        this.emit({ type: "refreshed", teamId: session.teamId, trigger, refreshedAt: this.now() });
        return;
      } catch (err) {
        if (err instanceof GabrielError && err.code === "AUTH_DEAD") {
          this.markDead("cookie_invalid");
          throw err;
        }
        lastErr = err;
        await sleep(500 * 2 ** attempt);
      }
    }
    // Transient failure: keep the old token, a later request will retry the refresh.
    this.stateValue = "VALID";
    throw lastErr instanceof GabrielError ? lastErr : new GabrielError("NETWORK_TIMEOUT", "token refresh failed", { cause: lastErr });
  }

  /** Enters the terminal AUTH_DEAD state. */
  markDead(reason: string): void {
    if (this.stateValue === "AUTH_DEAD") return;
    this.stateValue = "AUTH_DEAD";
    this.logger.error({ teamId: this.session?.teamId, reason }, "root session credential is dead");
    this.emit({ type: "dead", teamId: this.session?.teamId ?? "", reason });
  }

  private emit(evt: AuthEvent): void {
    for (const l of this.listeners) {
      try {
        l(evt);
      } catch (err) {
        this.logger.error({ err: String(err) }, "auth listener threw");
      }
    }
  }
}
