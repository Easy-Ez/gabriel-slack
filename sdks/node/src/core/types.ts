/** An unmodified downstream WebSocket frame. */
export interface RawWsFrame {
  type?: string;
  subtype?: string;
  [key: string]: unknown;
}

/** A raw frame whose `type === "message"`. */
export interface RawMessageFrame extends RawWsFrame {
  type: "message";
  channel?: string;
  user?: string;
  ts?: string;
  thread_ts?: string;
  text?: string;
  blocks?: unknown[];
}

export interface RequestOpts {
  /** Per-request timeout. Web API default 15s, EdgeAPI default 10s. */
  timeoutMs?: number;
  /** Cancels the request (rejects with CANCELLED). */
  signal?: AbortSignal;
  /**
   * Retry policy for transient failures. `auto` (default) retries read-only and
   * idempotent methods; `none` disables retries other than 429 cool-downs.
   */
  retry?: "auto" | "safe" | "none";
  /** Value for the `_x_reason` telemetry field. */
  reason?: string;
}

/** Minimal WebSocket surface the SDK needs (matches the `ws` package). */
export interface WebSocketLike {
  readonly readyState: number;
  send(data: string): void;
  close(code?: number, reason?: string): void;
  on(event: "open", listener: () => void): this;
  on(event: "message", listener: (data: unknown) => void): this;
  on(event: "close", listener: (code: number, reason: unknown) => void): this;
  on(event: "error", listener: (err: Error) => void): this;
  on(event: "unexpected-response", listener: (req: unknown, res: { statusCode?: number; headers?: Record<string, unknown> }) => void): this;
  removeAllListeners(): this;
}

export type WebSocketFactory = (url: string, headers: Record<string, string>) => WebSocketLike;

export type FetchLike = typeof fetch;

/** Observability hook. All label values are IDs or enums, never message content or credentials. */
export interface MetricsSink {
  increment(name: string, labels?: Record<string, string>): void;
  observe(name: string, value: number, labels?: Record<string, string>): void;
}

export const noopMetrics: MetricsSink = { increment() {}, observe() {} };
