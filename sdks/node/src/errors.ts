/**
 * Unified error model. Every public API rejects with a `GabrielError`.
 *
 * `retryable === true` means the error belongs to a transient class. The SDK has
 * already retried those internally, so when one reaches application code the
 * automatic retries are exhausted — do not wrap SDK calls in another retry loop.
 */
export type GabrielErrorCode =
  | "RATE_LIMITED"
  | "NETWORK_TIMEOUT"
  | "SOCKET_CLOSED"
  | "SERVER_ERROR"
  | "MESSAGE_NOT_FOUND_TRANSIENT"
  | "TOKEN_EXPIRED"
  | "AUTH_DEAD"
  | "NOT_IN_CHANNEL"
  | "CHANNEL_NOT_FOUND"
  | "PERMISSION_DENIED"
  | "MSG_TOO_LONG"
  | "MSG_BLOCKS_TOO_LONG"
  | "SUBSCRIPTION_CAP_EXCEEDED"
  | "SCOPE_POLICY_VIOLATION"
  | "FILE_TOO_LARGE"
  | "HANDLER_TIMEOUT"
  | "CANCELLED"
  | "STREAM_CLOSED"
  | "QUEUE_OVERFLOW"
  | "STATE_STORE_UNAVAILABLE"
  // Codes below are additions of the reference implementation (see sdks/node/README.md).
  | "LOOP_DETECTED"
  | "INVALID_ARGUMENT"
  | "INVALID_STATE"
  | "SLACK_API_ERROR"
  | "BROWSER_NOT_FOUND"
  | "SIGN_IN_TIMEOUT";

const RETRYABLE: ReadonlySet<GabrielErrorCode> = new Set<GabrielErrorCode>([
  "RATE_LIMITED",
  "NETWORK_TIMEOUT",
  "SOCKET_CLOSED",
  "SERVER_ERROR",
  "MESSAGE_NOT_FOUND_TRANSIENT",
  "TOKEN_EXPIRED",
]);

/** How one HTTP attempt of a request ended. */
export type AttemptOutcome = "timeout" | "network" | "server_error" | "rate_limited" | "slack_error" | "aborted";

/** One HTTP attempt that did not succeed. Never holds a URL, host or credential. */
export interface RequestAttempt {
  outcome: AttemptOutcome;
  /** From sending the request to the failure. */
  durationMs: number;
  /** Transport error code (`ECONNRESET`, `UND_ERR_CONNECT_TIMEOUT`), `HTTP 503`, or the Slack error string. */
  detail?: string;
}

export interface GabrielErrorOptions {
  rawSlackError?: string;
  retryAfterMs?: number;
  cause?: unknown;
  attempts?: readonly RequestAttempt[];
}

export class GabrielError extends Error {
  readonly code: GabrielErrorCode;
  readonly retryable: boolean;
  /** Slack's original error string, e.g. "channel_not_found". */
  readonly rawSlackError?: string;
  /** Suggested cool-down when rate limited. */
  readonly retryAfterMs?: number;
  /** The failed HTTP attempts behind a transport error or a missed send deadline, oldest first. */
  readonly attempts?: readonly RequestAttempt[];

  constructor(code: GabrielErrorCode, message: string, opts: GabrielErrorOptions = {}) {
    super(message, opts.cause === undefined ? undefined : { cause: opts.cause });
    this.name = "GabrielError";
    this.code = code;
    this.retryable = RETRYABLE.has(code);
    if (opts.rawSlackError !== undefined) this.rawSlackError = opts.rawSlackError;
    if (opts.retryAfterMs !== undefined) this.retryAfterMs = opts.retryAfterMs;
    if (opts.attempts !== undefined) this.attempts = [...opts.attempts];
  }
}

/** `#1 timeout after 15.0s; #2 network ECONNRESET after 0.3s` — the attempt list for an error message. */
export function describeAttempts(attempts: readonly RequestAttempt[]): string {
  if (attempts.length === 0) return "no attempt";
  const list = attempts
    .map((a, i) => `#${i + 1} ${a.outcome}${a.detail ? ` ${a.detail}` : ""} after ${(a.durationMs / 1000).toFixed(1)}s`)
    .join("; ");
  return `${attempts.length} attempt${attempts.length === 1 ? "" : "s"}: ${list}`;
}

/**
 * The most specific error code in a transport error's cause chain (`ECONNRESET`,
 * `UND_ERR_CONNECT_TIMEOUT`, `ENOTFOUND`), else the innermost error name. Never the message,
 * which can carry hosts.
 */
export function transportDetail(err: unknown): string | undefined {
  let name: string | undefined;
  let cur: unknown = err;
  for (let depth = 0; depth < 5 && cur !== null && typeof cur === "object"; depth++) {
    const code = (cur as { code?: unknown }).code;
    if (typeof code === "string" && /^[A-Z][A-Z0-9_]{1,40}$/.test(code)) return code;
    const n = (cur as { name?: unknown }).name;
    if (typeof n === "string" && /^[A-Za-z]{1,40}$/.test(n)) name = n;
    cur = (cur as { cause?: unknown }).cause;
  }
  return name;
}

export function isGabrielError(err: unknown, code?: GabrielErrorCode): err is GabrielError {
  return err instanceof GabrielError && (code === undefined || err.code === code);
}

/** Slack error strings that mean "the workspace token is no longer valid". */
export const TOKEN_INVALID_ERRORS: ReadonlySet<string> = new Set([
  "invalid_auth",
  "not_authed",
  "token_expired",
  "token_revoked",
]);

const SLACK_ERROR_MAP: Record<string, GabrielErrorCode> = {
  ratelimited: "RATE_LIMITED",
  rate_limited: "RATE_LIMITED",
  channel_not_found: "CHANNEL_NOT_FOUND",
  not_in_channel: "NOT_IN_CHANNEL",
  is_archived: "PERMISSION_DENIED",
  restricted_action: "PERMISSION_DENIED",
  not_allowed_token_type: "PERMISSION_DENIED",
  cant_update_message: "PERMISSION_DENIED",
  cant_delete_message: "PERMISSION_DENIED",
  cant_delete_file: "PERMISSION_DENIED",
  access_denied: "PERMISSION_DENIED",
  missing_scope: "PERMISSION_DENIED",
  no_permission: "PERMISSION_DENIED",
  account_inactive: "AUTH_DEAD",
  msg_too_long: "MSG_TOO_LONG",
  msg_blocks_too_long: "MSG_BLOCKS_TOO_LONG",
  internal_error: "SERVER_ERROR",
  fatal_error: "SERVER_ERROR",
  service_unavailable: "SERVER_ERROR",
  request_timeout: "NETWORK_TIMEOUT",
};

/** Maps a Slack `{ ok: false, error }` string to a `GabrielError`. */
export function fromSlackError(method: string, slackError: string): GabrielError {
  if (TOKEN_INVALID_ERRORS.has(slackError)) {
    return new GabrielError("TOKEN_EXPIRED", `${method}: ${slackError}`, { rawSlackError: slackError });
  }
  const code = SLACK_ERROR_MAP[slackError] ?? "SLACK_API_ERROR";
  return new GabrielError(code, `${method}: ${slackError}`, { rawSlackError: slackError });
}

export function cancelledError(reason?: unknown): GabrielError {
  if (reason instanceof GabrielError) return reason;
  const text = typeof reason === "string" ? reason : reason instanceof Error ? reason.message : "operation cancelled";
  return new GabrielError("CANCELLED", text, { cause: reason });
}
