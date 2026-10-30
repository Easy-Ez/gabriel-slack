import { randomBytes } from "node:crypto";
import { GabrielError, TOKEN_INVALID_ERRORS, cancelledError, fromSlackError } from "../errors.js";
import { anySignal, fullJitterDelay, sleep } from "../util/async.js";
import type { Logger } from "../util/logger.js";
import type { AuthManager, WorkspaceSession } from "./auth.js";
import type { ClientMetadata, Endpoints } from "./metadata.js";
import type { FetchLike, MetricsSink, RequestOpts } from "./types.js";

export const DEFAULT_WEB_API_TIMEOUT_MS = 15_000;
export const DEFAULT_EDGE_API_TIMEOUT_MS = 10_000;

const READ_ONLY_PREFIXES = [
  "conversations.history",
  "conversations.replies",
  "conversations.info",
  "client.",
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
];

/** Overwrite-style or naturally idempotent writes that are safe to replay. */
const IDEMPOTENT_WRITES = new Set([
  "chat.update",
  "reactions.add",
  "reactions.remove",
  "conversations.mark",
  "subscriptions.thread.mark",
  "subscriptions.thread.add",
  "subscriptions.thread.remove",
]);

export function isSafeToRetry(method: string, fields: Record<string, unknown>): boolean {
  if (READ_ONLY_PREFIXES.some((p) => method.startsWith(p))) return true;
  if (IDEMPOTENT_WRITES.has(method)) return true;
  // chat.postMessage is deduplicated server-side by client_msg_id.
  return method === "chat.postMessage" && typeof fields.client_msg_id === "string";
}

export interface HttpEngineDeps {
  fetch: FetchLike;
  auth: AuthManager;
  metadata: ClientMetadata;
  endpoints: Endpoints;
  logger: Logger;
  metrics: MetricsSink;
  maxRetries?: number;
  now?: () => number;
}

function encodeField(value: unknown): string | undefined {
  if (value === undefined || value === null) return undefined;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value);
}

const SLACK_HOST = /(^|\.)slack(-edge|-files)?\.com$/i;

/** Credentials go only to https Slack hosts, or to explicitly configured endpoint hosts (tests, proxies). */
function isTrustedHost(url: string, endpoints: Endpoints): boolean {
  const u = new URL(url);
  const configured = [endpoints.edgeApiBase, endpoints.workspaceApiBase, endpoints.authUrl].filter(Boolean).map((e) => new URL(e!).host);
  if (configured.includes(u.host)) return u.protocol === "https:" || u.hostname === "localhost" || u.hostname === "127.0.0.1";
  return u.protocol === "https:" && SLACK_HOST.test(u.hostname);
}

function xId(now: number): string {
  return `${randomBytes(4).toString("hex")}-${(now / 1000).toFixed(3)}`;
}

/**
 * Shared HTTP transport for Web API and EdgeAPI calls:
 * - default timeouts (Web API 15s, EdgeAPI 10s);
 * - one 429 cool-down bucket per method, shared by every caller;
 * - retries by idempotency class (reads, client_msg_id writes, overwrite writes);
 * - transparent token refresh + single replay on invalid_auth.
 */
export class HttpEngine {
  private readonly cooldownUntil = new Map<string, number>();
  private readonly maxRetries: number;
  private readonly now: () => number;

  constructor(private readonly deps: HttpEngineDeps) {
    this.maxRetries = deps.maxRetries ?? 3;
    this.now = deps.now ?? Date.now;
  }

  /** Calls `POST {workspace}/api/{method}` with the web client's multipart form encoding. */
  callWebApi<T = Record<string, unknown>>(method: string, fields: Record<string, unknown> = {}, opts: RequestOpts = {}): Promise<T> {
    if (!/^[a-zA-Z][a-zA-Z0-9_.]{1,100}$/.test(method)) {
      return Promise.reject(new GabrielError("INVALID_ARGUMENT", `invalid Web API method name`));
    }
    const safe = opts.retry === "none" ? false : opts.retry === "safe" ? true : isSafeToRetry(method, fields);
    return this.execute<T>(`web:${method}`, method, safe, opts, DEFAULT_WEB_API_TIMEOUT_MS, (session) => {
      const url = new URL(`api/${method}`, session.apiBase);
      url.searchParams.set("_x_id", xId(this.now()));
      url.searchParams.set("slack_route", session.teamId);
      url.searchParams.set("_x_version_ts", this.deps.metadata.agentVersion);
      url.searchParams.set("_x_gantry", "true");
      const form = new FormData();
      form.set("token", session.token);
      for (const [k, v] of Object.entries(fields)) {
        const encoded = encodeField(v);
        if (encoded !== undefined) form.set(k, encoded);
      }
      form.set("_x_reason", opts.reason ?? "gabriel-sdk");
      form.set("_x_mode", "online");
      form.set("_x_sonic", "true");
      form.set("_x_app_name", "client");
      return { url: url.toString(), init: { method: "POST", headers: this.headers(session), body: form } };
    });
  }

  /** Calls `POST {edgeApiBase}{teamId}/{endpoint}` with a JSON body. */
  callEdgeApi<T = Record<string, unknown>>(endpoint: string, payload: Record<string, unknown> = {}, opts: RequestOpts = {}): Promise<T> {
    if (!/^[a-zA-Z]+\/[a-zA-Z]+$/.test(endpoint)) {
      return Promise.reject(new GabrielError("INVALID_ARGUMENT", "invalid EdgeAPI endpoint (expected e.g. \"users/info\")"));
    }
    const safe = opts.retry !== "none";
    return this.execute<T>(`edge:${endpoint}`, endpoint, safe, opts, DEFAULT_EDGE_API_TIMEOUT_MS, (session) => {
      const url = new URL(`${session.teamId}/${endpoint}`, this.deps.endpoints.edgeApiBase);
      url.searchParams.set("_x_app_name", "client");
      url.searchParams.set("_x_num_retries", "0");
      return {
        url: url.toString(),
        init: {
          method: "POST",
          headers: { ...this.headers(session), "Content-Type": "application/json" },
          body: JSON.stringify({ token: session.token, ...payload }),
        },
      };
    });
  }

  /**
   * Authenticated raw request (file transfer). Adds cookie + bearer token only for
   * Slack-owned hosts; never sends credentials to arbitrary URLs.
   */
  async fetchAuthenticated(url: string, init: RequestInit & { signal?: AbortSignal }, opts: { bearer?: boolean } = {}): Promise<Response> {
    const target = new URL(url);
    if (target.protocol !== "https:" || !SLACK_HOST.test(target.hostname)) {
      throw new GabrielError("INVALID_ARGUMENT", `refusing to send session credentials to non-Slack host ${target.hostname}`);
    }
    const session = this.deps.auth.current();
    const headers: Record<string, string> = { ...this.headers(session), ...(init.headers as Record<string, string> | undefined) };
    if (opts.bearer !== false) headers.Authorization = `Bearer ${session.token}`;
    try {
      return await this.deps.fetch(target.toString(), { ...init, headers, redirect: "follow" });
    } catch (err) {
      if (init.signal?.aborted) throw cancelledError(init.signal.reason);
      throw new GabrielError("NETWORK_TIMEOUT", `request to ${target.hostname} failed`, { cause: err });
    }
  }

  private headers(session: WorkspaceSession): Record<string, string> {
    return {
      Cookie: `d=${session.cookieD}`,
      Origin: this.deps.metadata.origin,
      "User-Agent": this.deps.metadata.userAgent,
    };
  }

  private async execute<T>(
    bucket: string,
    label: string,
    safe: boolean,
    opts: RequestOpts,
    defaultTimeout: number,
    build: (session: WorkspaceSession) => { url: string; init: RequestInit },
  ): Promise<T> {
    let attempt = 0;
    let authReplayed = false;
    for (;;) {
      if (opts.signal?.aborted) throw cancelledError(opts.signal.reason);
      await this.waitCooldown(bucket, opts.signal);
      const session = this.deps.auth.current();
      const { url, init } = build(session);
      if (!isTrustedHost(url, this.deps.endpoints)) {
        throw new GabrielError("INVALID_ARGUMENT", `refusing to send session credentials to ${new URL(url).host}`);
      }
      const started = this.now();
      const timeoutSignal = AbortSignal.timeout(opts.timeoutMs ?? defaultTimeout);
      let resp: Response;
      try {
        resp = await this.deps.fetch(url, { ...init, signal: anySignal(timeoutSignal, opts.signal) });
      } catch (err) {
        if (opts.signal?.aborted) throw cancelledError(opts.signal.reason);
        const timedOut = timeoutSignal.aborted;
        const error = new GabrielError("NETWORK_TIMEOUT", `${label}: ${timedOut ? "request timed out" : "network error"}`, { cause: err });
        if (safe && attempt < this.maxRetries) {
          await sleep(fullJitterDelay(attempt++, 500, 8_000), opts.signal);
          continue;
        }
        throw error;
      } finally {
        this.deps.metrics.observe("api_request_latency_ms", this.now() - started, { method: label });
      }

      if (resp.status === 429) {
        const retryAfterMs = Math.max(1, Number(resp.headers.get("retry-after") ?? "5")) * 1000;
        this.cooldownUntil.set(bucket, this.now() + retryAfterMs);
        this.deps.metrics.increment("rate_limited_429_total", { method: label });
        this.deps.logger.warn({ method: label, retryAfterMs }, "rate limited");
        await resp.body?.cancel().catch(() => {});
        if (attempt < this.maxRetries) {
          attempt++;
          continue;
        }
        throw new GabrielError("RATE_LIMITED", `${label}: rate limited`, { retryAfterMs, rawSlackError: "ratelimited" });
      }
      if (resp.status >= 500) {
        await resp.body?.cancel().catch(() => {});
        if (safe && attempt < this.maxRetries) {
          await sleep(fullJitterDelay(attempt++, 500, 8_000), opts.signal);
          continue;
        }
        throw new GabrielError("SERVER_ERROR", `${label}: HTTP ${resp.status}`);
      }

      let body: Record<string, unknown>;
      try {
        body = (await resp.json()) as Record<string, unknown>;
      } catch (err) {
        throw new GabrielError("SERVER_ERROR", `${label}: HTTP ${resp.status} with a non-JSON body`, { cause: err });
      }
      if (body.ok === false) {
        const slackError = typeof body.error === "string" ? body.error : "unknown_error";
        if (TOKEN_INVALID_ERRORS.has(slackError) && !authReplayed) {
          authReplayed = true;
          await this.deps.auth.refresh(slackError === "token_expired" ? "token_expired" : "invalid_auth", session.token);
          continue;
        }
        if ((slackError === "ratelimited" || slackError === "rate_limited") && attempt < this.maxRetries) {
          this.cooldownUntil.set(bucket, this.now() + 5_000);
          attempt++;
          continue;
        }
        const mapped = fromSlackError(label, slackError);
        if (mapped.retryable && mapped.code !== "TOKEN_EXPIRED" && safe && attempt < this.maxRetries) {
          await sleep(fullJitterDelay(attempt++, 500, 8_000), opts.signal);
          continue;
        }
        throw mapped;
      }
      if (!resp.ok) throw new GabrielError("SLACK_API_ERROR", `${label}: HTTP ${resp.status}`);
      return body as T;
    }
  }

  private async waitCooldown(bucket: string, signal?: AbortSignal): Promise<void> {
    const until = this.cooldownUntil.get(bucket);
    if (until === undefined) return;
    const wait = until - this.now();
    if (wait <= 0) {
      this.cooldownUntil.delete(bucket);
      return;
    }
    await sleep(wait, signal);
  }
}
