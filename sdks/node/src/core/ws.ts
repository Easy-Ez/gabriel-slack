import { GabrielError } from "../errors.js";
import { fullJitterDelay } from "../util/async.js";
import type { Logger } from "../util/logger.js";
import { scrubSecrets } from "../util/redact.js";
import type { AuthManager } from "./auth.js";
import { buildStartArgs, type ClientMetadata, type Endpoints } from "./metadata.js";
import type { MetricsSink, RawWsFrame, WebSocketFactory, WebSocketLike } from "./types.js";

export const FIRST_SEQ_ID = 16385; // 0x4001, as used by the web client
export const PING_INTERVAL_MS = 10_000;
export const DEAD_PEER_TIMEOUT_MS = 25_000;
export const HELLO_TIMEOUT_MS = 10_000;
export const FAST_RECONNECT_TTL_MS = 285_000;
export const DEFAULT_WS_COMMAND_TIMEOUT_MS = 10_000;
const BACKOFF_BASE_MS = 1_000;
const BACKOFF_CAP_MS = 60_000;
const SERVER_BUSY_DEFAULT_MS = 30_000;
const OPEN = 1;

export type WsState =
  | "DISCONNECTED"
  | "CONNECTING_FAST"
  | "CONNECTING_COLD"
  | "AWAITING_HELLO"
  | "RESYNCING"
  | "READY"
  | "READY_DEGRADED"
  | "BACKOFF"
  | "CREDENTIAL_REFRESH"
  | "AUTH_DEAD"
  | "STOPPED";

export interface HelloInfo {
  epoch: number;
  fastReconnect: boolean;
  frame: RawWsFrame;
}

export interface WsConnectionEvents {
  frame(frame: RawWsFrame, epoch: number): void;
  state(state: WsState, prev: WsState): void;
  hello(info: HelloInfo): void;
  reconnecting(info: { attempt: number; delayMs: number }): void;
  closed(info: { epoch: number; code: number }): void;
}

export interface WsConnectionDeps {
  auth: AuthManager;
  metadata: ClientMetadata;
  endpoints: Endpoints;
  createWebSocket: WebSocketFactory;
  logger: Logger;
  metrics: MetricsSink;
  /** Gateway shard; defaults to `{teamId}-1`. */
  gatewayServer?: string;
  now?: () => number;
  random?: () => number;
}

interface PendingCommand {
  resolve: (frame: RawWsFrame) => void;
  reject: (err: unknown) => void;
  timer: NodeJS.Timeout;
  epoch: number;
}

const FRT_INVALID_MESSAGES = new Set(["Subscription failure from the CS", "Socket URL has expired"]);
const SERVER_BUSY_MESSAGES = new Set(["rate_limited", "ratelimited", "Ratelimited", "backend_overloaded", "unknown"]);

/**
 * WebSocket lifecycle state machine (spec/behavior.md §5.2):
 * connection epochs, 10s ping, 25s dead-peer detection, 285s fast-reconnect
 * tickets, `goodbye` (4104) hand-over, full-jitter backoff and credential refresh.
 */
export class WsConnection {
  private ws: WebSocketLike | undefined;
  private stateValue: WsState = "DISCONNECTED";
  private epoch = 0;
  private seq = FIRST_SEQ_ID;
  private readonly pending = new Map<number, PendingCommand>();
  private frtUrl: string | undefined;
  private frtReceivedAt = 0;
  private frtToken: string | undefined;
  private fastFailed = false;
  private attempt = 0;
  private lastFrameAt = 0;
  private stopped = true;
  private helloTimer: NodeJS.Timeout | undefined;
  private pingTimer: NodeJS.Timeout | undefined;
  private watchdogTimer: NodeJS.Timeout | undefined;
  private backoffTimer: NodeJS.Timeout | undefined;
  private nextDelayOverrideMs: number | undefined;
  private connectingFast = false;
  private readonly listeners: { [K in keyof WsConnectionEvents]: Set<WsConnectionEvents[K]> } = {
    frame: new Set(),
    state: new Set(),
    hello: new Set(),
    reconnecting: new Set(),
    closed: new Set(),
  };
  private readonly now: () => number;
  private readonly random: () => number;

  constructor(private readonly deps: WsConnectionDeps) {
    this.now = deps.now ?? Date.now;
    this.random = deps.random ?? Math.random;
  }

  get state(): WsState {
    return this.stateValue;
  }

  get currentEpoch(): number {
    return this.epoch;
  }

  get lastFrameAgeMs(): number {
    return this.lastFrameAt === 0 ? -1 : this.now() - this.lastFrameAt;
  }

  on<K extends keyof WsConnectionEvents>(event: K, listener: WsConnectionEvents[K]): () => void {
    (this.listeners[event] as Set<WsConnectionEvents[K]>).add(listener);
    return () => (this.listeners[event] as Set<WsConnectionEvents[K]>).delete(listener);
  }

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
  }

  /** Closes the socket with 1000 Normal Closure and stops reconnecting. */
  stop(): void {
    this.stopped = true;
    clearTimeout(this.backoffTimer);
    this.teardown(1000, "client_stop");
    this.setState("STOPPED");
  }

  /** Marks resync completion (called by the high-level client after gap recovery). */
  markReady(degraded: boolean, epoch?: number): void {
    if (epoch !== undefined && epoch !== this.epoch) return;
    if (this.stateValue === "RESYNCING" || this.stateValue === "READY" || this.stateValue === "READY_DEGRADED") {
      this.setState(degraded ? "READY_DEGRADED" : "READY");
      this.attempt = 0;
    }
  }

  /** Sends a frame without waiting for `reply_to`. Returns the allocated id. */
  send(frame: { type: string; [k: string]: unknown }): number {
    const ws = this.ws;
    if (!ws || ws.readyState !== OPEN || !this.isConnected()) {
      throw new GabrielError("SOCKET_CLOSED", "WebSocket is not connected");
    }
    const id = this.seq++;
    ws.send(JSON.stringify({ ...frame, id }));
    return id;
  }

  /** Sends a frame and resolves with the matching `reply_to` frame. */
  sendCommand<T = RawWsFrame>(frame: { type: string; [k: string]: unknown }, timeoutMs = DEFAULT_WS_COMMAND_TIMEOUT_MS): Promise<T> {
    let id: number;
    try {
      id = this.send(frame);
    } catch (err) {
      return Promise.reject(err);
    }
    return new Promise<T>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new GabrielError("NETWORK_TIMEOUT", `no reply_to for ${frame.type} within ${timeoutMs}ms`));
      }, timeoutMs);
      this.pending.set(id, { resolve: resolve as (f: RawWsFrame) => void, reject, timer, epoch: this.epoch });
    });
  }

  isConnected(): boolean {
    return this.stateValue === "RESYNCING" || this.stateValue === "READY" || this.stateValue === "READY_DEGRADED";
  }

  /** Forces a reconnect (e.g. after a token refresh). */
  reconnect(reason: string, opts: { cold?: boolean } = {}): void {
    if (opts.cold) this.clearFastReconnect();
    this.teardown(4000, reason);
    this.scheduleReconnect(true);
  }

  private connect(): void {
    if (this.stopped) return;
    let session;
    try {
      session = this.deps.auth.current();
    } catch {
      this.setState("AUTH_DEAD");
      return;
    }
    const useFast =
      this.frtUrl !== undefined &&
      !this.fastFailed &&
      this.frtToken === session.token &&
      this.now() - this.frtReceivedAt <= FAST_RECONNECT_TTL_MS;
    const url = useFast ? this.frtUrl! : this.coldUrl(session.token, session.teamId);
    this.connectingFast = useFast;
    const epoch = ++this.epoch;
    this.setState(useFast ? "CONNECTING_FAST" : "CONNECTING_COLD");

    const headers = {
      Cookie: `d=${session.cookieD}`,
      Origin: this.deps.metadata.origin,
      "User-Agent": this.deps.metadata.userAgent,
    };
    let ws: WebSocketLike;
    try {
      ws = this.deps.createWebSocket(url, headers);
    } catch (err) {
      this.deps.logger.warn({ err: scrubSecrets(String(err)) }, "websocket creation failed");
      this.onSocketGone(epoch, 1006);
      return;
    }
    this.ws = ws;

    ws.on("open", () => {
      if (epoch !== this.epoch) return;
      this.setState("AWAITING_HELLO");
      this.lastFrameAt = this.now();
      this.helloTimer = setTimeout(() => {
        if (epoch !== this.epoch) return;
        this.deps.logger.warn({ epoch }, "no hello within timeout");
        if (this.connectingFast) this.clearFastReconnect();
        this.teardown(4000, "hello_timeout");
        this.scheduleReconnect(false);
      }, HELLO_TIMEOUT_MS);
    });
    ws.on("message", (data) => {
      if (epoch !== this.epoch) return;
      this.lastFrameAt = this.now();
      let frame: RawWsFrame;
      try {
        frame = JSON.parse(typeof data === "string" ? data : String(data)) as RawWsFrame;
      } catch {
        this.deps.logger.warn({ epoch }, "dropping non-JSON frame");
        return;
      }
      this.handleFrame(frame, epoch);
    });
    ws.on("unexpected-response", (_req, res) => {
      if (epoch !== this.epoch) return;
      const status = res.statusCode ?? 0;
      this.deps.logger.warn({ status }, "websocket handshake rejected");
      if (status === 401 || status === 403) {
        this.teardown(4000, "handshake_auth");
        void this.refreshCredentials("invalid_auth");
        return;
      }
      if (status === 429) {
        const retryAfter = Number(res.headers?.["retry-after"] ?? 0);
        this.nextDelayOverrideMs = retryAfter > 0 ? retryAfter * 1000 : SERVER_BUSY_DEFAULT_MS;
      }
      if (this.connectingFast) this.clearFastReconnect();
      this.teardown(4000, "handshake_failed");
      this.scheduleReconnect(false);
    });
    ws.on("error", (err) => {
      if (epoch !== this.epoch) return;
      this.deps.logger.warn({ err: scrubSecrets(err.message) }, "websocket error");
    });
    ws.on("close", (code) => {
      if (epoch !== this.epoch) return;
      this.onSocketGone(epoch, code);
    });
  }

  private coldUrl(token: string, teamId: string): string {
    const params = new URLSearchParams({
      token,
      sync_desync: "1",
      slack_client: "desktop",
      start_args: buildStartArgs(this.deps.metadata.agentVersion),
      no_query_on_subscribe: "1",
      flannel: "3",
      lazy_channels: "1",
      gateway_server: this.deps.gatewayServer ?? `${teamId}-1`,
      batch_presence_aware: "1",
    });
    return `${this.deps.endpoints.wsBase}?${params.toString()}`;
  }

  private handleFrame(frame: RawWsFrame, epoch: number): void {
    if (typeof frame.reply_to === "number") {
      const pending = this.pending.get(frame.reply_to);
      if (pending) {
        this.pending.delete(frame.reply_to);
        clearTimeout(pending.timer);
        if (frame.ok === false) {
          const errObj = (frame.error ?? {}) as { msg?: string; code?: number };
          pending.reject(
            new GabrielError("SLACK_API_ERROR", `ws command rejected: ${errObj.msg ?? "error"}`, {
              rawSlackError: errObj.msg ?? (typeof frame.error === "string" ? frame.error : undefined),
              cause: frame,
            }),
          );
        } else {
          pending.resolve(frame);
        }
      }
    }

    switch (frame.type) {
      case "hello": {
        clearTimeout(this.helloTimer);
        const fastReconnect = frame.fast_reconnect === true;
        this.fastFailed = false;
        this.setState("RESYNCING");
        this.startHeartbeat(epoch);
        this.deps.metrics.increment("ws_hello_total", { fast: String(fastReconnect) });
        for (const l of this.listeners.hello) l({ epoch, fastReconnect, frame });
        break;
      }
      case "reconnect_url":
        if (typeof frame.url === "string") {
          this.frtUrl = frame.url;
          this.frtReceivedAt = this.now();
          this.frtToken = this.safeToken();
        }
        break;
      case "goodbye":
        this.deps.logger.info({ epoch }, "server goodbye; switching connection");
        this.teardown(4104, "goodbye");
        this.scheduleReconnect(true);
        break;
      case "error":
        this.handleErrorFrame(frame);
        break;
    }

    for (const l of this.listeners.frame) {
      try {
        l(frame, epoch);
      } catch (err) {
        this.deps.logger.error({ err: String(err) }, "frame listener threw");
      }
    }
  }

  private handleErrorFrame(frame: RawWsFrame): void {
    const err = (frame.error ?? {}) as { msg?: string; code?: number; retry_after?: number };
    const msg = err.msg ?? "";
    this.deps.logger.warn({ msg, code: err.code }, "websocket error frame");
    if (FRT_INVALID_MESSAGES.has(msg) || (msg === "invalid_auth" && this.connectingFast)) {
      this.clearFastReconnect();
      return;
    }
    if (msg === "invalid_auth") {
      this.teardown(4000, "invalid_auth");
      void this.refreshCredentials("invalid_auth");
      return;
    }
    if (SERVER_BUSY_MESSAGES.has(msg)) {
      this.nextDelayOverrideMs = err.retry_after ? err.retry_after * 1000 : SERVER_BUSY_DEFAULT_MS;
    }
  }

  private async refreshCredentials(trigger: "invalid_auth" | "token_expired"): Promise<void> {
    this.setState("CREDENTIAL_REFRESH");
    const failedToken = this.safeToken();
    try {
      await this.deps.auth.refresh(trigger, failedToken);
      this.clearFastReconnect();
      clearTimeout(this.backoffTimer);
      this.attempt = 0;
      this.setState("DISCONNECTED");
      this.connect();
    } catch (err) {
      if (err instanceof GabrielError && err.code === "AUTH_DEAD") {
        this.stopped = true;
        this.setState("AUTH_DEAD");
        return;
      }
      this.scheduleReconnect(false);
    }
  }

  private safeToken(): string | undefined {
    try {
      return this.deps.auth.current().token;
    } catch {
      return undefined;
    }
  }

  private startHeartbeat(epoch: number): void {
    this.stopHeartbeat();
    this.pingTimer = setInterval(() => {
      if (epoch !== this.epoch) return;
      try {
        this.send({ type: "ping" });
      } catch {
        /* socket closing; the close handler reconnects */
      }
    }, PING_INTERVAL_MS);
    this.watchdogTimer = setInterval(() => {
      if (epoch !== this.epoch) return;
      if (this.now() - this.lastFrameAt > DEAD_PEER_TIMEOUT_MS) {
        this.deps.logger.warn({ epoch, lastFrameAgeMs: this.now() - this.lastFrameAt }, "dead peer detected");
        this.teardown(4000, "dead_peer");
        this.scheduleReconnect(true);
      }
    }, 5_000);
  }

  private stopHeartbeat(): void {
    clearInterval(this.pingTimer);
    clearInterval(this.watchdogTimer);
    this.pingTimer = undefined;
    this.watchdogTimer = undefined;
  }

  private onSocketGone(epoch: number, code: number): void {
    const wasConnected = this.isConnected();
    if (this.connectingFast && !wasConnected) this.clearFastReconnect();
    this.teardown(code, "closed");
    for (const l of this.listeners.closed) l({ epoch, code });
    if (this.stopped || this.stateValue === "AUTH_DEAD" || this.stateValue === "CREDENTIAL_REFRESH") return;
    this.scheduleReconnect(wasConnected);
  }

  /** Detaches the current socket, rejects pending commands and stops timers. */
  private teardown(code: number, reason: string): void {
    clearTimeout(this.helloTimer);
    this.stopHeartbeat();
    const ws = this.ws;
    this.ws = undefined;
    // Invalidate the epoch so late callbacks from the old socket are ignored.
    if (ws) {
      this.epoch++;
      ws.removeAllListeners();
      ws.on("error", () => {});
      try {
        ws.close(code, reason);
      } catch {
        /* already closed */
      }
    }
    for (const [id, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(new GabrielError("SOCKET_CLOSED", "WebSocket closed before reply"));
      this.pending.delete(id);
    }
  }

  private clearFastReconnect(): void {
    this.frtUrl = undefined;
    this.frtToken = undefined;
    this.fastFailed = true;
  }

  /** `immediate` skips backoff for a graceful hand-over from a healthy connection. */
  private scheduleReconnect(immediate: boolean): void {
    if (this.stopped) return;
    clearTimeout(this.backoffTimer);
    const override = this.nextDelayOverrideMs;
    this.nextDelayOverrideMs = undefined;
    // Only a drop from a healthy (READY) connection reconnects without delay; repeated
    // failures before READY fall back to full-jitter backoff to avoid a reconnect storm.
    const delayMs =
      override ?? (immediate && this.attempt === 0 ? 0 : fullJitterDelay(this.attempt, BACKOFF_BASE_MS, BACKOFF_CAP_MS, this.random));
    this.attempt++;
    this.setState("BACKOFF");
    this.deps.metrics.increment("ws_reconnect_total");
    for (const l of this.listeners.reconnecting) l({ attempt: this.attempt, delayMs });
    this.backoffTimer = setTimeout(() => {
      this.setState("DISCONNECTED");
      this.connect();
    }, delayMs);
  }

  private setState(next: WsState): void {
    const prev = this.stateValue;
    if (prev === next) return;
    this.stateValue = next;
    for (const l of this.listeners.state) l(next, prev);
  }
}
