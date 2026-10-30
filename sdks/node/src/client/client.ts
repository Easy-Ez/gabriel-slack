import { GabrielCore, type GabrielCoreOptions } from "../core/core.js";
import type { AuthOptions } from "../core/auth.js";
import type { ClientMetadata, Endpoints } from "../core/metadata.js";
import type { FetchLike, MetricsSink, RawWsFrame, WebSocketFactory } from "../core/types.js";
import { noopMetrics } from "../core/types.js";
import type { WsState } from "../core/ws.js";
import { GabrielError, cancelledError } from "../errors.js";
import { badgeSnapshotKey, normalizeFrame, type Draft } from "../events/normalize.js";
import type { GabrielEvent, GabrielEventMap, GabrielEventType, MessageCreatedEvent, UnreadBadgeCountsEvent } from "../events/types.js";
import { MemoryStateStore, type StateStore } from "../state/store.js";
import { withTimeout } from "../util/async.js";
import type { Logger } from "../util/logger.js";
import { silentLogger } from "../util/logger.js";
import { LruCache } from "../util/lru.js";
import { compareTs, isSubteamId } from "../util/validate.js";
import { ChannelsApi, FilesApi, ReactionsApi, SearchApi, ThreadsApi, UsersApi } from "./apis.js";
import { ChatApi, type ChatSchedulerOptions, type CreateStreamOptions, type SendOptions, type SendResult } from "./chat.js";
import {
  WaitRegistry,
  assertWritable,
  currentTrigger,
  matcherToPredicate,
  triggerOf,
  triggerStorage,
  type EventContext,
  type MessageMatcher,
  type ReplyOptions,
  type WaitForReplyOptions,
} from "./context.js";
import { KeyedExecutor, type JobHandle } from "./executor.js";
import { Hydrator, type HydrationResult } from "./hydrator.js";
import { attachLLMContext } from "./llm-context.js";
import { LoopBreaker, SelfRegistry } from "./origin.js";
import { PresenceApi } from "./presence.js";
import { WatermarkTracker, recoverGap } from "./recovery.js";
import { ScopedClient, type ScopePolicy } from "./scope.js";
import type { StreamController } from "./stream.js";
import { UnreadCursorGuard } from "./unread.js";

export interface ConcurrencyOptions {
  /** Handlers running at once across all keys (default 32). */
  globalConcurrency?: number;
  /** Handlers running at once per executor key; 1 = strictly serial (default 1). */
  perKeyConcurrency?: number;
  /** Executor queue bound (default 5000). */
  maxQueue?: number;
  /** In-flight event bound (hydration + queued + running). Excess is dropped with guard.overflow (default 10000). */
  maxInflightEvents?: number;
  /** 'channel' (default): serial per channel; 'thread': serial per thread, parallel across threads. */
  executorKey?: "channel" | "thread";
  /** Per-handler time limit, 0 disables (default 300000). waitForReply time is not counted. */
  handlerTimeoutMs?: number;
}

export interface HydrationOptions {
  /** Time budget for resolving names per event (default 300ms). */
  hydrateBudgetMs?: number;
  /** Preload user groups so @group mentions resolve and `isMentioningMe` covers my groups (default true). */
  preloadUsergroups?: boolean;
}

export interface LoopGuardOptions {
  /** Deliver echoes of this client's own writes (`origin: 'this_client'`). Default false. */
  includeSelfMessages?: boolean;
  /** Automatic writes allowed per (channel, thread) per 60s (default 5). */
  maxAutoRepliesPerMinute?: number;
}

export interface RecoveryOptions {
  /** History pages fetched per channel during gap recovery (default 10). */
  maxPagesPerChannel?: number;
  /** Threads whose replies are recovered directly (LRU, default 100). */
  watchedThreadsLimit?: number;
  /** Max realtime frames buffered while resyncing (default 10000). */
  resyncBufferLimit?: number;
}

export interface GabrielClientOptions {
  auth: AuthOptions;
  concurrency?: ConcurrencyOptions;
  hydration?: HydrationOptions;
  loopGuard?: LoopGuardOptions;
  chat?: ChatSchedulerOptions;
  recovery?: RecoveryOptions;
  /** Defaults to MemoryStateStore. */
  stateStore?: StateStore;
  logger?: Logger;
  metrics?: MetricsSink;
  /** Advanced: client identity, endpoints, transport injection (proxies, tests). */
  advanced?: {
    metadata?: Partial<ClientMetadata>;
    endpoints?: Partial<Endpoints>;
    fetch?: FetchLike;
    createWebSocket?: WebSocketFactory;
    gatewayServer?: string;
    startTimeoutMs?: number;
  };
}

export interface HealthReport {
  status: "healthy" | "degraded" | "dead";
  wsState: WsState;
  authState: string;
  lastFrameAgeMs: number;
  queueDepth: number;
}

export type Handler<E> = (evt: E, ctx: EventContext) => void | Promise<void>;
export type Middleware = (evt: GabrielEvent, ctx: EventContext, next: () => Promise<void>) => Promise<void>;
export type ErrorHandler = (err: unknown, evt?: GabrielEvent) => void;

interface Routed {
  draft: Draft;
  hydration: HydrationResult;
}

const KNOWN_MESSAGE_SUBTYPES = new Set([
  "file_share", "thread_broadcast", "bot_message", "me_message", "message_changed", "message_deleted", "message_replied",
  "channel_join", "channel_leave", "channel_topic", "channel_purpose", "channel_name", "channel_archive", "channel_unarchive",
  "channel_convert_to_private", "channel_convert_to_public", "group_join", "group_leave", "group_topic", "group_purpose",
  "group_name", "group_archive", "group_unarchive", "pinned_item", "unpinned_item", "bot_add", "bot_remove", "tombstone",
  "reminder_add", "huddle_thread", "sh_room_created", "joiner_notification", "file_comment", "ekm_access_denied",
  "slackbot_response", "assistant_app_thread", "channel_canvas_updated", "bot_enable", "bot_disable",
]);

const CONTROL_TYPES = new Set(["hello", "pong", "reconnect_url", "goodbye", "error"]);

/**
 * High-level semantic client: ordered, hydrated, loop-guarded events and domain APIs,
 * built entirely on {@link GabrielCore}.
 */
export class GabrielClient {
  readonly core: GabrielCore;
  readonly chat: ChatApi;
  readonly threads: ThreadsApi;
  readonly files: FilesApi;
  readonly search: SearchApi;
  readonly presence: PresenceApi;
  readonly reactions: ReactionsApi;
  readonly users: UsersApi;
  readonly channels: ChannelsApi;

  private readonly logger: Logger;
  private readonly metrics: MetricsSink;
  private readonly store: StateStore;
  private readonly hydrator: Hydrator;
  private readonly registry: SelfRegistry;
  private readonly loopBreaker: LoopBreaker;
  private readonly executor: KeyedExecutor<Routed>;
  private readonly waits = new WaitRegistry();
  private readonly marks: WatermarkTracker;
  private readonly unread: UnreadCursorGuard;
  private readonly handlers = new Map<string, Set<Handler<GabrielEvent>>>();
  private readonly middlewares: Middleware[] = [];
  private readonly errorHandlers = new Set<ErrorHandler>();
  private readonly pullQueues = new Set<{ push(evt: GabrielEvent): void }>();
  private readonly seen = new LruCache<string, true>(10_000, 30 * 60_000);
  private readonly lanes = new Map<string, Promise<void>>();
  /** "channel:ts" → abort controllers of handlers triggered by that message. */
  private readonly inflightByTs = new Map<string, Set<AbortController>>();
  private readonly allControllers = new Set<AbortController>();
  private readonly opts: Required<Pick<ConcurrencyOptions, "maxInflightEvents" | "handlerTimeoutMs" | "executorKey">> & {
    hydrateBudgetMs: number;
    includeSelfMessages: boolean;
    maxPagesPerChannel: number;
    resyncBufferLimit: number;
  };
  private inflightEvents = 0;
  private helloEpoch = 0;
  private readonly completed = new WeakSet<Draft>();
  private lastBadgeKey: string | undefined;
  private resyncBuffer: Draft[] | undefined;
  private connectedOnce = false;
  private stopping = false;
  private started = false;
  private eventSeq = 0;
  private readyWaiters: Array<{ resolve: () => void; reject: (e: unknown) => void }> = [];
  private reconcileTimer: NodeJS.Timeout | undefined;
  private readonly preloadUsergroups: boolean;

  /** Recommended entry point: only the session cookie is required. */
  static fromCookie(opts: AuthOptions & Omit<GabrielClientOptions, "auth">): GabrielClient {
    const { cookieD, token, workspace, allowedTeams, ...rest } = opts;
    return new GabrielClient({
      ...rest,
      auth: { cookieD, ...(token ? { token } : {}), ...(workspace ? { workspace } : {}), ...(allowedTeams ? { allowedTeams } : {}) },
    });
  }

  constructor(options: GabrielClientOptions) {
    if (!options || !options.auth) throw new GabrielError("INVALID_ARGUMENT", "GabrielClient requires `auth`");
    this.logger = options.logger ?? silentLogger;
    this.metrics = options.metrics ?? noopMetrics;
    this.store = options.stateStore ?? new MemoryStateStore();
    const adv = options.advanced ?? {};
    const coreOpts: GabrielCoreOptions = {
      auth: options.auth,
      logger: this.logger,
      metrics: this.metrics,
      ...(adv.metadata ? { metadata: adv.metadata } : {}),
      ...(adv.endpoints ? { endpoints: adv.endpoints } : {}),
      ...(adv.fetch ? { fetch: adv.fetch } : {}),
      ...(adv.createWebSocket ? { createWebSocket: adv.createWebSocket } : {}),
      ...(adv.gatewayServer ? { gatewayServer: adv.gatewayServer } : {}),
    };
    this.core = new GabrielCore(coreOpts);
    this.startTimeoutMs = adv.startTimeoutMs ?? 60_000;
    const c = options.concurrency ?? {};
    this.opts = {
      maxInflightEvents: c.maxInflightEvents ?? 10_000,
      handlerTimeoutMs: c.handlerTimeoutMs ?? 300_000,
      executorKey: c.executorKey ?? "channel",
      hydrateBudgetMs: options.hydration?.hydrateBudgetMs ?? 300,
      includeSelfMessages: options.loopGuard?.includeSelfMessages ?? false,
      maxPagesPerChannel: options.recovery?.maxPagesPerChannel ?? 10,
      resyncBufferLimit: options.recovery?.resyncBufferLimit ?? 10_000,
    };
    this.preloadUsergroups = options.hydration?.preloadUsergroups ?? true;
    this.executor = new KeyedExecutor<Routed>({
      globalConcurrency: c.globalConcurrency ?? 32,
      perKeyConcurrency: c.perKeyConcurrency ?? 1,
      maxQueue: c.maxQueue ?? 5000,
    });
    this.hydrator = new Hydrator(this.core, this.logger);
    this.registry = new SelfRegistry(() => this.core.session.userId);
    this.loopBreaker = new LoopBreaker(options.loopGuard?.maxAutoRepliesPerMinute ?? 5);
    this.marks = new WatermarkTracker(this.store, () => this.core.session.teamId, options.recovery?.watchedThreadsLimit ?? 100, this.logger);
    this.unread = new UnreadCursorGuard(this.core, this.registry, this.logger);
    this.chat = new ChatApi(
      this.core,
      this.registry,
      {
        beforeAutoWrite: (channel, threadTs) => this.checkLoop(channel, threadTs),
        withUnreadGuard: (channel, enabled, fn) => this.unread.run(channel, enabled, fn),
        noteThreadActivity: (channel, threadTs) => this.marks.watchThread(channel, threadTs),
      },
      this.logger,
      options.chat,
    );
    this.threads = new ThreadsApi(
      this.core,
      this.hydrator,
      (ch, ts) => this.marks.watchThread(ch, ts),
      (ch, ts) => this.marks.unwatchThread(ch, ts),
    );
    this.files = new FilesApi(this.core, this.hydrator, (id) => this.registry.registerClientMsgId(id));
    this.search = new SearchApi(this.core, this.hydrator);
    this.reactions = new ReactionsApi(this.core);
    this.users = new UsersApi(this.hydrator);
    this.channels = new ChannelsApi(this.core, this.hydrator);
    this.presence = new PresenceApi(this.core, this.hydrator, (evt, lane) => this.emitSynthetic(evt, lane), this.logger);

    this.core.onRawFrame((frame) => this.onFrame(frame));
    this.core.onHello((info) => void this.onHello(info.epoch, info.fastReconnect));
    this.core.ws.on("reconnecting", ({ attempt, delayMs }) =>
      this.emitSynthetic({ type: "connection.reconnecting", eventId: this.syntheticId("connection.reconnecting"), attempt, delayMs }, "__connection__"),
    );
    this.core.ws.on("state", (state) => {
      if (state === "AUTH_DEAD") this.failReadyWaiters(new GabrielError("AUTH_DEAD", "root session credential is no longer valid"));
    });
    this.core.onAuthEvent((evt) => {
      if (evt.type === "refreshed") {
        this.emitSynthetic({ type: "auth.refreshed", eventId: this.syntheticId("auth.refreshed"), teamId: evt.teamId, trigger: evt.trigger, refreshedAt: evt.refreshedAt }, "__auth__");
      } else {
        this.emitSynthetic({ type: "auth.dead", eventId: this.syntheticId("auth.dead"), teamId: evt.teamId, reason: evt.reason }, "__auth__");
        this.failReadyWaiters(new GabrielError("AUTH_DEAD", `root session credential is no longer valid (${evt.reason})`));
        this.core.ws.stop();
      }
    });
    this.core.onProtocolDrift((info) =>
      this.emitSynthetic({ type: "guard.protocol_drift", eventId: this.syntheticId("guard.protocol_drift"), ...info }, "__guard__"),
    );
  }

  private readonly startTimeoutMs: number;

  /** Current user ID (available after `start()`). */
  get selfUserId(): string {
    return this.core.session.userId;
  }

  get teamId(): string {
    return this.core.session.teamId;
  }

  // -------------------------------------------------------------------------------------------
  // lifecycle

  /** Exchanges credentials, connects, and resolves on the first `connection.ready`. */
  async start(): Promise<void> {
    if (this.started) throw new GabrielError("INVALID_STATE", "client already started");
    this.started = true;
    this.stopping = false;
    let session;
    try {
      session = await this.core.initialize();
    } catch (err) {
      this.started = false;
      throw err;
    }
    await this.marks.load();
    if (this.preloadUsergroups) {
      await withTimeout(this.hydrator.preloadUsergroups(session.userId), 5_000, () => undefined).catch(() => {});
    }
    const ready = new Promise<void>((resolve, reject) => this.readyWaiters.push({ resolve, reject }));
    await this.core.connect();
    try {
      await withTimeout(ready, this.startTimeoutMs, () => new GabrielError("NETWORK_TIMEOUT", `not connected within ${this.startTimeoutMs}ms`));
    } catch (err) {
      await this.stop({ drainMs: 0 }).catch(() => {});
      throw err;
    }
  }

  /**
   * Graceful shutdown: stop intake → drain handlers (abort after `drainMs`) → interrupt
   * open streams → drain sends → persist state → close with 1000.
   */
  async stop(opts: { drainMs?: number } = {}): Promise<void> {
    if (this.stopping) return;
    this.stopping = true;
    const drainMs = opts.drainMs ?? 10_000;
    clearTimeout(this.reconcileTimer);
    // Events still hydrating are dropped by route() (and pinned so a later recovery refetches them).
    await withTimeout(Promise.allSettled([...this.lanes.values()]), Math.max(1_000, drainMs), () => undefined).catch(() => {});
    try {
      await withTimeout(this.executor.idle(), Math.max(0, drainMs), () => new Error("drain timeout"));
    } catch {
      for (const c of this.allControllers) c.abort(cancelledError("client stopping"));
      await withTimeout(this.executor.idle(), 1_000, () => undefined).catch(() => {});
    }
    await this.chat.interruptAll().catch(() => {});
    await withTimeout(this.chat.drain(), 5_000, () => undefined).catch(() => {});
    await this.marks.flush().catch((err) => this.logger.warn({ err: String(err) }, "watermark flush failed"));
    await this.store.close().catch(() => {});
    this.presence.stop();
    this.core.disconnect();
    for (const q of this.pullQueues) (q as unknown as { close(): void }).close?.();
    this.started = false;
  }

  health(): HealthReport {
    const wsState = this.core.wsState;
    const authState = this.core.authManager.state;
    const status: HealthReport["status"] =
      authState === "AUTH_DEAD" ? "dead" : wsState === "READY" ? "healthy" : "degraded";
    return { status, wsState, authState, lastFrameAgeMs: this.core.ws.lastFrameAgeMs, queueDepth: this.executor.queueDepth };
  }

  /** Least-privilege view for LLM tools. */
  scope(policy: ScopePolicy): ScopedClient {
    return new ScopedClient(
      policy,
      { chat: this.chat, threads: this.threads, files: this.files, search: this.search, reactions: this.reactions, users: this.users, channels: this.channels, presence: this.presence },
      this.registry,
      this.logger,
    );
  }

  // -------------------------------------------------------------------------------------------
  // subscriptions

  on<K extends GabrielEventType>(type: K, handler: Handler<GabrielEventMap[K]>): () => void {
    let set = this.handlers.get(type);
    if (!set) this.handlers.set(type, (set = new Set()));
    set.add(handler as Handler<GabrielEvent>);
    return () => set!.delete(handler as Handler<GabrielEvent>);
  }

  /** Declarative `message.created` routing. */
  onMessage(matcher: MessageMatcher, handler: Handler<MessageCreatedEvent>): () => void {
    const predicate = matcherToPredicate(matcher);
    return this.on("message.created", (evt, ctx) => (predicate(evt) ? handler(evt, ctx) : undefined));
  }

  /** Thread replies only (filter over `message.created`). */
  onThreadReply(handler: Handler<MessageCreatedEvent>): () => void {
    return this.on("message.created", (evt, ctx) => (evt.isThreadReply ? handler(evt, ctx) : undefined));
  }

  onError(handler: ErrorHandler): () => void {
    this.errorHandlers.add(handler);
    return () => this.errorHandlers.delete(handler);
  }

  /** Onion middleware around every handler invocation. */
  use(mw: Middleware): void {
    this.middlewares.push(mw);
  }

  /** Pull-mode event stream (in dispatch order). */
  events(opts: { bufferSize?: number } = {}): AsyncIterableIterator<GabrielEvent> {
    const limit = opts.bufferSize ?? 10_000;
    const buffer: GabrielEvent[] = [];
    let waiting: ((r: IteratorResult<GabrielEvent>) => void) | undefined;
    let closed = false;
    const queue = {
      push: (evt: GabrielEvent) => {
        if (closed) return;
        if (waiting) {
          const w = waiting;
          waiting = undefined;
          w({ value: evt, done: false });
        } else if (buffer.length < limit) buffer.push(evt);
        else this.logger.warn({ type: evt.type }, "events() consumer too slow; dropping event");
      },
      close: () => {
        closed = true;
        this.pullQueues.delete(queue);
        if (waiting) waiting({ value: undefined as never, done: true });
      },
    };
    this.pullQueues.add(queue);
    const iterator: AsyncIterableIterator<GabrielEvent> = {
      next: () => {
        if (buffer.length > 0) return Promise.resolve({ value: buffer.shift()!, done: false });
        if (closed) return Promise.resolve({ value: undefined as never, done: true });
        return new Promise((resolve) => (waiting = resolve));
      },
      return: () => {
        queue.close();
        return Promise.resolve({ value: undefined as never, done: true });
      },
      [Symbol.asyncIterator]: () => iterator,
    };
    return iterator;
  }

  // -------------------------------------------------------------------------------------------
  // inbound pipeline

  private onFrame(frame: RawWsFrame): void {
    if (this.stopping) return;
    const type = frame.type;
    if (typeof type !== "string" || CONTROL_TYPES.has(type)) return;
    if (typeof frame.reply_to === "number") return;
    this.metrics.increment("events_received_total", { type });
    if (this.presence.handleFrame(frame)) return;

    switch (type) {
      case "subteam_self_added":
        if (isSubteamId(frame.subteam_id)) this.hydrator.mySubteams.add(frame.subteam_id);
        return;
      case "subteam_self_removed":
        if (isSubteamId(frame.subteam_id)) this.hydrator.mySubteams.delete(frame.subteam_id);
        return;
      case "subteam_members_changed":
        this.onSubteamMembersChanged(frame);
        return;
      case "subteam_updated":
      case "subteam_created": {
        const st = (frame.subteam ?? {}) as { id?: string; handle?: string; name?: string; users?: string[] };
        if (st.id) this.hydrator.upsertUsergroup({ id: st.id, handle: st.handle ?? "", ...(st.name ? { name: st.name } : {}), ...(st.users ? { users: st.users } : {}) });
        break;
      }
      case "channel_rename":
      case "group_rename":
      case "im_created":
      case "channel_archive":
      case "group_archive": {
        const ch = frame.channel;
        const id = typeof ch === "string" ? ch : (ch as { id?: string } | undefined)?.id;
        if (id) this.hydrator.invalidateChannel(id);
        break;
      }
      case "channel_marked":
      case "im_marked":
      case "group_marked":
      case "mpim_marked":
        if (typeof frame.channel === "string" && typeof frame.ts === "string") {
          this.unread.onMarked(frame.channel, frame.ts);
          if (this.registry.isSelfMark(frame.channel, frame.ts)) return; // SDK-caused echo
        }
        break;
      case "message":
        this.onMessageFrame(frame);
        break;
    }

    const draft = normalizeFrame(frame, this.normalizeContext("realtime"));
    if (!draft) return;
    if (draft.event.type === "message.other" && !KNOWN_MESSAGE_SUBTYPES.has(draft.event.rawSubtype)) {
      this.core.reportDrift("message", draft.event.rawSubtype, frame);
    }
    if (draft.event.type === "unread.badge_counts") {
      const key = badgeSnapshotKey(draft.event as UnreadBadgeCountsEvent);
      if (key === this.lastBadgeKey) return;
      this.lastBadgeKey = key;
    }
    this.dispatch(draft, "realtime");
  }

  private onMessageFrame(frame: RawWsFrame): void {
    const channel = typeof frame.channel === "string" ? frame.channel : undefined;
    const subtype = typeof frame.subtype === "string" ? frame.subtype : undefined;
    if (subtype === "message_deleted" && channel) {
      // Control-plane bypass: cancel in-flight work for the deleted message immediately.
      const deletedTs = typeof frame.deleted_ts === "string" ? frame.deleted_ts : typeof frame.ts === "string" ? frame.ts : undefined;
      if (deletedTs) {
        for (const c of this.inflightByTs.get(`${channel}:${deletedTs}`) ?? []) c.abort(cancelledError("message_deleted"));
        this.chat.noteDeleted(deletedTs);
      }
      return;
    }
    const msg = subtype === "message_changed" ? ((frame.message ?? {}) as Record<string, unknown>) : frame;
    const ts = typeof msg.ts === "string" ? msg.ts : undefined;
    if (ts) this.hydrator.recordThreadRoot(ts, typeof msg.thread_ts === "string" ? msg.thread_ts : null);
    const cmid = typeof msg.client_msg_id === "string" ? msg.client_msg_id : undefined;
    if (cmid && ts && msg.user === this.core.session.userId) this.chat.noteEcho(cmid, ts);
  }

  private onSubteamMembersChanged(frame: RawWsFrame): void {
    const id = typeof frame.subteam_id === "string" ? frame.subteam_id : undefined;
    if (!id) return;
    const added = Array.isArray(frame.added_users) ? (frame.added_users as string[]) : [];
    const removed = new Set(Array.isArray(frame.removed_users) ? (frame.removed_users as string[]) : []);
    const self = this.core.session.userId;
    if (added.includes(self)) this.hydrator.mySubteams.add(id);
    if (removed.has(self)) this.hydrator.mySubteams.delete(id);
    const rec = this.hydrator.usergroup(id);
    if (!rec?.users) return;
    const users = [...rec.users.filter((u) => !removed.has(u)), ...added.filter((u) => !rec.users!.includes(u))];
    this.hydrator.upsertUsergroup({ ...rec, users });
    this.emitSynthetic(
      { type: "user.subteam_updated", eventId: `subteam:${id}:${String(frame.event_ts ?? this.syntheticId("subteam"))}`, subteamId: id, handle: rec.handle, memberIds: users },
      id,
    );
  }

  private normalizeContext(delivery: "realtime" | "backfill") {
    return {
      selfUserId: this.core.session.userId,
      classifyOrigin: (info: Parameters<SelfRegistry["classify"]>[0]) => this.registry.classify(info),
      isMySubteam: (id: string) => this.hydrator.mySubteams.has(id),
      delivery,
    };
  }

  private executorKeyOf(draft: Draft): string {
    const base = draft.channelId ?? draft.laneKey;
    if (this.opts.executorKey === "thread" && draft.channelId && draft.threadKey) return `${draft.channelId}:${draft.threadKey}`;
    return base;
  }

  private dispatch(draft: Draft, delivery: "realtime" | "backfill"): void {
    if (this.resyncBuffer && delivery === "realtime") {
      if (this.resyncBuffer.length >= this.opts.resyncBufferLimit) {
        this.overflow(draft);
        return;
      }
      this.resyncBuffer.push(draft);
      return;
    }
    const eventId = draft.event.eventId;
    if (draft.dedupe && this.seen.has(eventId)) return;
    if (this.inflightEvents >= this.opts.maxInflightEvents) {
      this.overflow(draft);
      return;
    }
    if (draft.dedupe) this.seen.set(eventId, true);
    this.inflightEvents++;
    const threadTs = this.threadOfDraft(draft);
    if (draft.createdTs && draft.channelId) this.marks.begin(draft.channelId, draft.createdTs, threadTs);
    const hydration = this.hydrator.hydrate(draft, this.opts.hydrateBudgetMs);
    const lane = draft.laneKey;
    const prev = this.lanes.get(lane) ?? Promise.resolve();
    const next = prev
      .then(() => hydration)
      .then((h) => this.route(draft, h))
      .catch((err) => {
        this.logger.error({ err: String(err), type: draft.event.type }, "dispatch failed");
        this.complete(draft);
      });
    this.lanes.set(lane, next);
    void next.then(() => {
      if (this.lanes.get(lane) === next) this.lanes.delete(lane);
    });
  }

  private threadOfDraft(draft: Draft): string | undefined {
    const evt = draft.event as { thread?: { rootTs: string } | null };
    return evt.thread?.rootTs;
  }

  private overflow(draft: Draft): void {
    this.metrics.increment("events_dropped_total", { type: draft.event.type });
    if (draft.createdTs && draft.channelId) this.marks.pin(draft.channelId, draft.createdTs, this.threadOfDraft(draft));
    this.seen.delete(draft.event.eventId);
    this.emitGuard({
      type: "guard.overflow",
      eventId: this.syntheticId("guard.overflow"),
      ...(draft.channelId ? { channelId: draft.channelId } : {}),
      ...(draft.createdTs ? { ts: draft.createdTs } : {}),
    });
  }

  private complete(draft: Draft): void {
    if (this.completed.has(draft)) return;
    this.completed.add(draft);
    this.inflightEvents = Math.max(0, this.inflightEvents - 1);
    if (draft.createdTs && draft.channelId) this.marks.complete(draft.channelId, draft.createdTs, this.threadOfDraft(draft));
  }

  private route(draft: Draft, hydration: HydrationResult): void {
    const evt = draft.event;
    if (this.stopping && !evt.type.startsWith("auth.") && !evt.type.startsWith("connection.")) {
      if (draft.createdTs && draft.channelId) this.marks.pin(draft.channelId, draft.createdTs, this.threadOfDraft(draft));
      this.completed.add(draft);
      this.inflightEvents = Math.max(0, this.inflightEvents - 1);
      return;
    }
    if (evt.type === "message.created") attachLLMContext(evt);
    this.metrics.observe("hydration_latency_ms", hydration.complete ? 0 : this.opts.hydrateBudgetMs, { type: evt.type });
    if ((evt.type === "message.created" || evt.type === "message.updated") && evt.origin === "this_client" && !this.opts.includeSelfMessages) {
      this.complete(draft);
      return;
    }
    if (this.waits.route(evt)) {
      this.complete(draft);
      return;
    }
    for (const q of this.pullQueues) q.push(evt);
    const handlers = this.handlers.get(evt.type);
    if (!handlers || handlers.size === 0) {
      this.complete(draft);
      return;
    }
    const submitted = this.executor.submit(this.executorKeyOf(draft), { draft, hydration }, (job) =>
      this.runHandlers([...handlers], draft, hydration, job).finally(() => this.complete(draft)),
    );
    if (!submitted) {
      this.inflightEvents = Math.max(0, this.inflightEvents - 1);
      this.overflow(draft);
    }
  }

  /** Synthetic (client-generated) events go through the same lanes, without hydration. */
  private emitSynthetic(evt: GabrielEvent, laneKey: string): void {
    if (this.stopping && evt.type !== "auth.dead") return;
    const draft: Draft = {
      event: evt,
      laneKey,
      needs: { users: new Set(), channels: new Set(), usergroups: new Set() },
      dedupe: false,
      render() {},
    };
    this.inflightEvents++;
    const lane = laneKey;
    const prev = this.lanes.get(lane) ?? Promise.resolve();
    const next = prev.then(() => this.route(draft, { complete: true, remaining: async () => {} }));
    this.lanes.set(lane, next);
    void next.then(() => {
      if (this.lanes.get(lane) === next) this.lanes.delete(lane);
    });
  }

  private emitGuard(evt: GabrielEvent): void {
    // Guard events bypass bounds so overflow can always be observed.
    for (const q of this.pullQueues) q.push(evt);
    const handlers = this.handlers.get(evt.type);
    if (!handlers) return;
    for (const h of handlers) {
      Promise.resolve()
        .then(() => h(evt, this.detachedContext(evt)))
        .catch((err) => this.reportError(err, evt));
    }
  }

  private syntheticId(kind: string): string {
    return `${kind}:${Date.now()}:${++this.eventSeq}`;
  }

  private async runHandlers(handlers: Array<Handler<GabrielEvent>>, draft: Draft, hydration: HydrationResult, job: JobHandle): Promise<void> {
    for (const handler of handlers) {
      if (this.stopping && this.allControllers.size === 0 && draft.event.type.startsWith("connection.")) break;
      await this.runOne(handler, draft, hydration, job);
    }
  }

  private async runOne(handler: Handler<GabrielEvent>, draft: Draft, hydration: HydrationResult, job: JobHandle): Promise<void> {
    const evt = draft.event;
    const controller = new AbortController();
    this.allControllers.add(controller);
    const trigger = triggerOf(evt);
    const tsKey = trigger.channel && trigger.ts ? `${trigger.channel}:${trigger.ts}` : undefined;
    if (tsKey) {
      let set = this.inflightByTs.get(tsKey);
      if (!set) this.inflightByTs.set(tsKey, (set = new Set()));
      set.add(controller);
    }
    let writesCancelled = false;
    let remaining = this.opts.handlerTimeoutMs;
    let timerStartedAt = Date.now();
    let timer: NodeJS.Timeout | undefined;
    let graceTimer: NodeJS.Timeout | undefined;
    const armTimer = () => {
      if (this.opts.handlerTimeoutMs <= 0) return;
      timerStartedAt = Date.now();
      timer = setTimeout(() => {
        const err = new GabrielError("HANDLER_TIMEOUT", `handler exceeded ${this.opts.handlerTimeoutMs}ms`);
        controller.abort(err);
        this.reportError(err, evt);
        graceTimer = setTimeout(() => {
          writesCancelled = true;
          job.forceRelease();
          this.complete(draft); // a timed-out event counts as handled (failed); watermarks advance
        }, 5_000);
      }, Math.max(0, remaining));
    };
    const pauseTimer = () => {
      if (!timer) return;
      clearTimeout(timer);
      timer = undefined;
      remaining -= Date.now() - timerStartedAt;
    };

    const ctx = this.makeContext(evt, controller, hydration, () => writesCancelled, {
      suspend: () => {
        pauseTimer();
        job.suspend();
      },
      resume: async () => {
        await job.resume();
        if (!controller.signal.aborted) armTimer();
      },
    });
    armTimer();
    try {
      await triggerStorage.run(trigger, () => this.compose(handler)(evt, ctx));
    } catch (err) {
      if (!(err instanceof GabrielError && err.code === "CANCELLED" && controller.signal.aborted)) this.reportError(err, evt);
    } finally {
      clearTimeout(timer);
      clearTimeout(graceTimer);
      this.allControllers.delete(controller);
      if (tsKey) {
        const set = this.inflightByTs.get(tsKey);
        set?.delete(controller);
        if (set && set.size === 0) this.inflightByTs.delete(tsKey);
      }
    }
  }

  private compose(handler: Handler<GabrielEvent>): (evt: GabrielEvent, ctx: EventContext) => Promise<void> {
    const mws = this.middlewares;
    return (evt, ctx) => {
      let index = -1;
      const dispatch = async (i: number): Promise<void> => {
        if (i <= index) throw new GabrielError("INVALID_STATE", "next() called multiple times");
        index = i;
        if (i === mws.length) {
          await handler(evt, ctx);
          return;
        }
        await mws[i]!(evt, ctx, () => dispatch(i + 1));
      };
      return dispatch(0);
    };
  }

  private reportError(err: unknown, evt?: GabrielEvent): void {
    if (this.errorHandlers.size === 0) {
      this.logger.error({ err: err instanceof Error ? `${err.name}: ${err.message}` : String(err), eventId: evt?.eventId, type: evt?.type }, "handler error");
      return;
    }
    for (const h of this.errorHandlers) {
      try {
        h(err, evt);
      } catch (inner) {
        this.logger.error({ err: String(inner) }, "onError handler threw");
      }
    }
  }

  private makeContext(
    evt: GabrielEvent,
    controller: AbortController,
    hydration: HydrationResult,
    writesCancelled: () => boolean,
    job: { suspend(): void; resume(): Promise<void> },
  ): EventContext {
    const trigger = triggerOf(evt);
    const where = (inThread: boolean) => {
      if (!trigger.channel) throw new GabrielError("INVALID_STATE", `${evt.type} has no channel to reply to`);
      return { channel: trigger.channel, ...(inThread && trigger.threadRootTs ? { threadTs: trigger.threadRootTs } : {}) };
    };
    const signal = controller.signal;
    return {
      signal,
      reply: (markdownOrOpts: string | ReplyOptions): Promise<SendResult> => {
        assertWritable(writesCancelled() || signal.aborted);
        const o = typeof markdownOrOpts === "string" ? { markdown: markdownOrOpts } : markdownOrOpts;
        const send: SendOptions = {
          ...where(o.inThread !== false),
          markdown: o.markdown,
          priority: "interactive",
          ...(o.broadcast !== undefined ? { broadcast: o.broadcast } : {}),
          ...(o.preserveUnreadCursor !== undefined ? { preserveUnreadCursor: o.preserveUnreadCursor } : {}),
        };
        return this.chat.send(send, signal);
      },
      replyStream: (o = {}): Promise<StreamController> => {
        assertWritable(writesCancelled() || signal.aborted);
        const opts: CreateStreamOptions = {
          ...where(true),
          ...(o.initialMarkdown !== undefined ? { initialMarkdown: o.initialMarkdown } : {}),
          ...(o.preserveUnreadCursor !== undefined ? { preserveUnreadCursor: o.preserveUnreadCursor } : {}),
        };
        return this.chat.createStream(opts, signal);
      },
      react: async (emoji: string): Promise<void> => {
        assertWritable(writesCancelled() || signal.aborted);
        if (!trigger.channel || !trigger.ts) throw new GabrielError("INVALID_STATE", `${evt.type} has no message to react to`);
        await this.reactions.add({ channel: trigger.channel, ts: trigger.ts, emoji });
      },
      waitForReply: (filter, opts: WaitForReplyOptions) => this.waitForReply(evt, filter, opts, signal, job),
      hydrateRemaining: () => hydration.remaining(),
    };
  }

  private detachedContext(evt: GabrielEvent): EventContext {
    const controller = new AbortController();
    return this.makeContext(evt, controller, { complete: true, remaining: async () => {} }, () => false, { suspend() {}, resume: async () => {} });
  }

  private async waitForReply(
    trigger: GabrielEvent,
    filter: MessageMatcher | ((evt: MessageCreatedEvent) => boolean),
    opts: WaitForReplyOptions,
    signal: AbortSignal,
    job: { suspend(): void; resume(): Promise<void> },
  ): Promise<MessageCreatedEvent | null> {
    if (!opts || typeof opts.timeoutMs !== "number" || opts.timeoutMs <= 0) {
      throw new GabrielError("INVALID_ARGUMENT", "waitForReply: opts.timeoutMs must be a positive number");
    }
    const predicate = matcherToPredicate(filter);
    const consume = opts.consume !== false;
    const afterTs = (trigger as { ts?: string }).ts;
    // 1. Already queued for this executor key?
    const t = triggerOf(trigger);
    const draftLike = { channelId: t.channel, laneKey: t.channel ?? "", threadKey: t.threadRootTs } as Draft;
    const key = this.executorKeyOf(draftLike);
    const pending = this.executor.extractPending(key, (m) => {
      const e = m.draft.event;
      return e.type === "message.created" && (!afterTs || compareTs(e.ts, afterTs) > 0) && safe(predicate, e);
    });
    if (pending) {
      if (!consume) {
        // Not consuming: leave normal handling intact by re-submitting.
        const handlers = this.handlers.get("message.created");
        if (handlers?.size) {
          this.executor.submit(key, pending, (j) => this.runHandlers([...handlers], pending.draft, pending.hydration, j).finally(() => this.complete(pending.draft)));
        } else this.complete(pending.draft);
      } else this.complete(pending.draft);
      return pending.draft.event as MessageCreatedEvent;
    }
    // 2. Recently routed but unconsumed?
    const recent = this.waits.takeRecent(predicate, afterTs, consume);
    if (recent) {
      if (consume) {
        const queued = this.executor.extractPending(key, (m) => m.draft.event.eventId === recent.eventId);
        if (queued) this.complete(queued.draft);
      }
      return recent;
    }
    // 3. Register and wait without holding executor permits.
    if (signal.aborted) throw cancelledError(signal.reason);
    job.suspend();
    let remove: (() => void) | undefined;
    let timer: NodeJS.Timeout | undefined;
    try {
      return await new Promise<MessageCreatedEvent | null>((resolve, reject) => {
        const onAbort = () => reject(cancelledError(signal.reason));
        signal.addEventListener("abort", onAbort, { once: true });
        timer = setTimeout(() => resolve(null), opts.timeoutMs);
        remove = this.waits.add({
          predicate,
          consume,
          exclusive: opts.exclusive === true,
          ...(afterTs ? { afterTs } : {}),
          resolve: (e) => {
            signal.removeEventListener("abort", onAbort);
            resolve(e);
          },
        });
      });
    } finally {
      clearTimeout(timer);
      remove?.();
      await job.resume();
    }
  }

  private checkLoop(channel: string, threadTs: string | undefined): void {
    if (!currentTrigger()) return; // only automatic (handler-driven) writes count
    const { allowed, count } = this.loopBreaker.tryRecord(channel, threadTs);
    if (allowed) return;
    this.emitGuard({
      type: "guard.loop_detected",
      eventId: this.syntheticId("guard.loop_detected"),
      channelId: channel,
      ...(threadTs ? { threadTs } : {}),
      replyCountInWindow: count,
    });
    throw new GabrielError("LOOP_DETECTED", `auto-reply circuit open for ${channel}${threadTs ? `/${threadTs}` : ""} (${count} writes in 60s)`);
  }

  // -------------------------------------------------------------------------------------------
  // connection lifecycle

  private async onHello(epoch: number, fastReconnect: boolean): Promise<void> {
    this.helloEpoch = epoch;
    this.presence.onHello(epoch);
    const hadConnection = this.connectedOnce;
    this.connectedOnce = true;
    const needsColdResync = !fastReconnect && (hadConnection || this.store.persistent || this.marks.hasPinned());
    if (fastReconnect || !needsColdResync) {
      this.flushResyncBuffer(); // a resync interrupted by this reconnect must not strand its buffer
      this.core.ws.markReady(false, epoch);
      this.emitReady(fastReconnect, false);
      if (fastReconnect) {
        clearTimeout(this.reconcileTimer);
        this.reconcileTimer = setTimeout(() => void this.runRecovery("fast_reconnect_reconcile", false), 2_000);
        this.reconcileTimer.unref?.();
      }
      return;
    }
    // Keep (merge into) the buffer of a resync this reconnect interrupted.
    this.resyncBuffer ??= [];
    let degraded = false;
    try {
      degraded = !(await this.runRecovery(this.marks.hasPinned() && hadConnection ? "overflow" : "cold_resync", true));
    } finally {
      // A newer hello owns the buffer and the READY transition.
      if (this.helloEpoch === epoch) this.flushResyncBuffer();
    }
    if (this.helloEpoch !== epoch) return;
    this.core.ws.markReady(degraded, epoch);
    this.emitReady(false, degraded);
  }

  private flushResyncBuffer(): void {
    const buffered = this.resyncBuffer ?? [];
    this.resyncBuffer = undefined;
    for (const d of buffered) this.dispatch(d, "realtime");
  }

  /** Returns true when the gap was fully recovered. */
  private async runRecovery(cause: "fast_reconnect_reconcile" | "cold_resync" | "overflow", blocking: boolean): Promise<boolean> {
    try {
      const { frames, gap } = await recoverGap(this.core, this.channels, this.marks, { maxPagesPerChannel: this.opts.maxPagesPerChannel, logger: this.logger });
      const firstBuffered = this.resyncBuffer?.find((d) => d.createdTs)?.createdTs;
      const late: Draft[] = [];
      for (const frame of frames) {
        const draft = normalizeFrame(frame, this.normalizeContext("backfill"));
        if (!draft) continue;
        if (blocking && firstBuffered && draft.createdTs && compareTs(draft.createdTs, firstBuffered) >= 0) late.push(draft);
        else this.dispatch(draft, "backfill");
      }
      if (late.length && this.resyncBuffer) this.resyncBuffer.push(...late);
      for (const ch of gap.channelsRecovered) this.marks.unpinAll(ch);
      if (gap.recoveredCount > 0 || !gap.recovered) {
        this.emitSynthetic({ type: "connection.gap", eventId: this.syntheticId("connection.gap"), cause, ...gap }, "__connection__");
      }
      return gap.recovered;
    } catch (err) {
      this.logger.warn({ err: String(err), cause }, "gap recovery failed");
      return false;
    }
  }

  private emitReady(fastReconnect: boolean, degraded: boolean): void {
    this.emitSynthetic({ type: "connection.ready", eventId: this.syntheticId("connection.ready"), fastReconnect, degraded }, "__connection__");
    const waiters = this.readyWaiters;
    this.readyWaiters = [];
    for (const w of waiters) w.resolve();
  }

  private failReadyWaiters(err: unknown): void {
    const waiters = this.readyWaiters;
    this.readyWaiters = [];
    for (const w of waiters) w.reject(err);
  }
}

function safe(predicate: (evt: MessageCreatedEvent) => boolean, evt: MessageCreatedEvent): boolean {
  try {
    return predicate(evt) === true;
  } catch {
    return false;
  }
}
