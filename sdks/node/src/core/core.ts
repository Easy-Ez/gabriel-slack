import WebSocket from "ws";
import { GabrielError } from "../errors.js";
import type { Logger } from "../util/logger.js";
import { silentLogger } from "../util/logger.js";
import { AuthManager, exchangeCookie, findTeam, type AuthEvent, type AuthOptions, type CookieExchangeResult, type WorkspaceSession } from "./auth.js";
import { HttpEngine } from "./http.js";
import { DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS, type ClientMetadata, type Endpoints } from "./metadata.js";
import { noopMetrics, type FetchLike, type MetricsSink, type RawMessageFrame, type RawWsFrame, type RequestOpts, type WebSocketFactory, type WebSocketLike } from "./types.js";
import { WsConnection, type HelloInfo, type WsState } from "./ws.js";

/** Frame types the reference implementation knows about; anything else is reported as protocol drift. */
export const KNOWN_FRAME_TYPES: ReadonlySet<string> = new Set([
  "hello", "pong", "reconnect_url", "goodbye", "error",
  "message", "presence_change", "manual_presence_change", "user_typing",
  "reaction_added", "reaction_removed", "user_change", "user_invalidated", "user_status_changed",
  "dnd_updated", "dnd_updated_user", "dnd_invalidated",
  "subteam_created", "subteam_updated", "subteam_members_changed", "subteam_self_added", "subteam_self_removed",
  "channel_marked", "im_marked", "group_marked", "mpim_marked", "thread_marked",
  "activity", "badge_counts_updated", "clear_mention_notification", "desktop_notification",
  "draft_create", "draft_change", "draft_send", "draft_delete",
  "member_joined_channel", "member_left_channel", "channel_joined", "channel_left", "group_joined", "group_left",
  "im_created", "im_open", "im_close", "mpim_open", "mpim_close", "mpim_joined",
  "channel_created", "channel_deleted", "channel_rename", "group_rename", "channel_archive", "group_archive",
  "channel_unarchive", "group_unarchive", "channel_updated", "channel_history_changed",
  "file_shared", "file_change", "file_public", "file_deleted", "file_created", "file_unshared",
  "file_comment_added", "file_comment_edited", "file_comment_deleted",
  "pin_added", "pin_removed", "star_added", "star_removed", "emoji_changed", "pref_change",
  "subscription_update", "flannel", "ai_assistant_status", "agent_session",
  "update_thread_state", "thread_subscribed", "thread_unsubscribed",
  "user_huddle_changed", "sh_room_join", "sh_room_leave", "sh_room_update",
  "team_join", "team_rename", "team_pref_change", "team_profile_change", "bot_added", "bot_changed",
  "apps_changed", "apps_installed", "apps_uninstalled", "commands_changed",
  "mobile_in_app_notification", "reminder_added", "reminder_fired", "reminder_completed",
  // Observed on live connections (client bookkeeping, no high-level event).
  "visits_changed", "file_view_ready",
]);

export interface GabrielCoreOptions {
  auth: AuthOptions;
  metadata?: Partial<ClientMetadata>;
  endpoints?: Partial<Endpoints>;
  /** Custom fetch (proxies, tests). Defaults to the global `fetch`. */
  fetch?: FetchLike;
  /** Custom WebSocket factory (proxies, tests). Defaults to the `ws` package. */
  createWebSocket?: WebSocketFactory;
  /** Gateway shard override, defaults to `{teamId}-1`. */
  gatewayServer?: string;
  logger?: Logger;
  metrics?: MetricsSink;
  /** Max automatic retries for transient HTTP failures (default 3). */
  maxHttpRetries?: number;
}

export type ProtocolDriftInfo = { unknownType: string; unknownSubtype?: string; sampleFrame: RawWsFrame };

const defaultWebSocketFactory: WebSocketFactory = (url, headers) =>
  new WebSocket(url, { headers, perMessageDeflate: false, handshakeTimeout: 15_000 }) as unknown as WebSocketLike;

/**
 * Low-level protocol core: fidelity, reliability and zero business assumptions.
 * Frames are delivered unmodified; the high-level client is built entirely on
 * these primitives and exposes them as `client.core` for escape-hatch use.
 */
export class GabrielCore {
  readonly http: HttpEngine;
  readonly ws: WsConnection;
  readonly authManager: AuthManager;
  readonly metadata: ClientMetadata;
  readonly endpoints: Endpoints;
  private readonly logger: Logger;
  private readonly rawHandlers = new Set<(frame: RawWsFrame) => void>();
  private readonly typeHandlers = new Map<string, Set<(frame: RawWsFrame) => void>>();
  private readonly subtypeHandlers = new Map<string, Set<(frame: RawMessageFrame) => void>>();
  private readonly driftHandlers = new Set<(info: ProtocolDriftInfo) => void>();
  private readonly seenUnknown = new Set<string>();
  private readonly fetchImpl: FetchLike;
  private started = false;

  constructor(opts: GabrielCoreOptions) {
    this.logger = opts.logger ?? silentLogger;
    this.metadata = { ...DEFAULT_CLIENT_METADATA, ...opts.metadata };
    this.endpoints = { ...DEFAULT_ENDPOINTS, ...opts.endpoints };
    this.fetchImpl = opts.fetch ?? fetch;
    const metrics = opts.metrics ?? noopMetrics;
    this.authManager = new AuthManager(opts.auth, {
      fetch: this.fetchImpl,
      metadata: this.metadata,
      endpoints: this.endpoints,
      logger: this.logger,
    });
    this.http = new HttpEngine({
      fetch: this.fetchImpl,
      auth: this.authManager,
      metadata: this.metadata,
      endpoints: this.endpoints,
      logger: this.logger,
      metrics,
      ...(opts.maxHttpRetries === undefined ? {} : { maxRetries: opts.maxHttpRetries }),
    });
    this.ws = new WsConnection({
      auth: this.authManager,
      metadata: this.metadata,
      endpoints: this.endpoints,
      createWebSocket: opts.createWebSocket ?? defaultWebSocketFactory,
      logger: this.logger,
      metrics,
      ...(opts.gatewayServer === undefined ? {} : { gatewayServer: opts.gatewayServer }),
    });
    this.ws.on("frame", (frame) => this.dispatchFrame(frame));
  }

  /** Credential helpers (`exchangeCookie` returns tokens — never log the result). */
  readonly auth = {
    exchangeCookie: (cookieD: string): Promise<CookieExchangeResult> =>
      exchangeCookie({ fetch: this.fetchImpl, metadata: this.metadata, endpoints: this.endpoints }, cookieD),
    findTeam: (domain: string): Promise<Record<string, unknown>> =>
      findTeam({ fetch: this.fetchImpl, metadata: this.metadata, endpoints: this.endpoints }, domain),
    test: (): Promise<Record<string, unknown>> => this.callWebApi("auth.test", {}),
  };

  get session(): WorkspaceSession {
    return this.authManager.current();
  }

  get wsState(): WsState {
    return this.ws.state;
  }

  /** Resolves the workspace session. Idempotent. */
  async initialize(): Promise<WorkspaceSession> {
    if (this.authManager.state !== "UNINITIALIZED") return this.authManager.current();
    return this.authManager.initialize(async (provisional) => {
      // Token supplied explicitly: verify the pairing and derive identity via auth.test.
      const resp = await this.fetchImpl(new URL("api/auth.test", provisional.apiBase), {
        method: "POST",
        headers: { Cookie: `d=${provisional.cookieD}`, Origin: this.metadata.origin, "User-Agent": this.metadata.userAgent },
        body: new URLSearchParams({ token: provisional.token }),
        signal: AbortSignal.timeout(15_000),
      });
      const body = (await resp.json().catch(() => ({}))) as { ok?: boolean; team_id?: string; user_id?: string; error?: string };
      if (!body.ok || !body.team_id || !body.user_id) {
        throw new GabrielError("AUTH_DEAD", `auth.test rejected the supplied token (${body.error ?? `HTTP ${resp.status}`})`);
      }
      return { teamId: body.team_id, userId: body.user_id };
    });
  }

  /** Initializes auth (if needed) and starts the WebSocket state machine. */
  async connect(): Promise<void> {
    await this.initialize();
    if (this.started) return;
    this.started = true;
    this.ws.start();
  }

  disconnect(): void {
    this.started = false;
    this.ws.stop();
  }

  callWebApi<T = Record<string, unknown>>(method: string, fields: Record<string, unknown> = {}, opts?: RequestOpts): Promise<T> {
    return this.http.callWebApi<T>(method, fields, opts);
  }

  callEdgeApi<T = Record<string, unknown>>(endpoint: string, payload: Record<string, unknown> = {}, opts?: RequestOpts): Promise<T> {
    return this.http.callEdgeApi<T>(endpoint, payload, opts);
  }

  /** Sends an upstream command, allocating `id` and awaiting `reply_to`. */
  sendWsCommand<T = RawWsFrame>(frame: { type: string; [k: string]: unknown }, timeoutMs?: number): Promise<T> {
    return this.ws.sendCommand<T>(frame, timeoutMs);
  }

  /** Sends an upstream frame without awaiting a reply. Returns the allocated id. */
  sendWsFrame(frame: { type: string; [k: string]: unknown }): number {
    return this.ws.send(frame);
  }

  onRawFrame(handler: (frame: RawWsFrame) => void): () => void {
    this.rawHandlers.add(handler);
    return () => this.rawHandlers.delete(handler);
  }

  onEvent(type: string, handler: (frame: RawWsFrame) => void): () => void {
    let set = this.typeHandlers.get(type);
    if (!set) this.typeHandlers.set(type, (set = new Set()));
    set.add(handler);
    return () => set!.delete(handler);
  }

  /** `subtype === undefined` subscribes to plain messages without a subtype. */
  onMessageSubtype(subtype: string | undefined, handler: (frame: RawMessageFrame) => void): () => void {
    const key = subtype ?? "";
    let set = this.subtypeHandlers.get(key);
    if (!set) this.subtypeHandlers.set(key, (set = new Set()));
    set.add(handler);
    return () => set!.delete(handler);
  }

  onProtocolDrift(handler: (info: ProtocolDriftInfo) => void): () => void {
    this.driftHandlers.add(handler);
    return () => this.driftHandlers.delete(handler);
  }

  onHello(handler: (info: HelloInfo) => void): () => void {
    return this.ws.on("hello", handler);
  }

  onAuthEvent(handler: (evt: AuthEvent) => void): () => void {
    return this.authManager.onEvent(handler);
  }

  private dispatchFrame(frame: RawWsFrame): void {
    const safe = (fn: () => void) => {
      try {
        fn();
      } catch (err) {
        this.logger.error({ err: String(err), type: frame.type }, "raw frame handler threw");
      }
    };
    for (const h of this.rawHandlers) safe(() => h(frame));
    const type = typeof frame.type === "string" ? frame.type : undefined;
    if (type) {
      for (const h of this.typeHandlers.get(type) ?? []) safe(() => h(frame));
      if (type === "message") {
        const sub = typeof frame.subtype === "string" ? frame.subtype : "";
        for (const h of this.subtypeHandlers.get(sub) ?? []) safe(() => h(frame as RawMessageFrame));
      }
      if (!KNOWN_FRAME_TYPES.has(type)) this.reportDrift(type, undefined, frame);
    }
  }

  /** Reports an unknown frame type/subtype once per process. */
  reportDrift(unknownType: string, unknownSubtype: string | undefined, frame: RawWsFrame): void {
    const key = `${unknownType}/${unknownSubtype ?? ""}`;
    if (this.seenUnknown.has(key)) return;
    this.seenUnknown.add(key);
    this.logger.info({ unknownType, unknownSubtype }, "protocol drift: unknown frame");
    for (const h of this.driftHandlers) {
      try {
        h({ unknownType, ...(unknownSubtype === undefined ? {} : { unknownSubtype }), sampleFrame: frame });
      } catch (err) {
        this.logger.error({ err: String(err) }, "drift handler threw");
      }
    }
  }
}
