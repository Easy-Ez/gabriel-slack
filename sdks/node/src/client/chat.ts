import { randomUUID } from "node:crypto";
import type { GabrielCore } from "../core/core.js";
import { GabrielError } from "../errors.js";
import { contentHash } from "../events/normalize.js";
import { chunkMarkdown, DEFAULT_CHUNK_BUDGET, DEFAULT_CHUNK_MIN } from "../markdown/chunker.js";
import { anySignal, sleep } from "../util/async.js";
import type { Logger } from "../util/logger.js";
import { LruCache } from "../util/lru.js";
import { PrioritySemaphore, TokenBucket, type SendPriority } from "../util/semaphore.js";
import { assertChannelId, assertTs, isTs } from "../util/validate.js";
import type { SelfRegistry } from "./origin.js";
import { StreamControllerImpl, type StreamController } from "./stream.js";

export interface SendOptions {
  channel: string;
  /** Standard Markdown (sent as `markdown_text`). Exactly one of markdown / blocks. */
  markdown?: string;
  blocks?: unknown[];
  threadTs?: string;
  /** Also post a thread reply to the channel. */
  broadcast?: boolean;
  priority?: "interactive" | "normal";
  /** Restore the channel's unread cursor after sending (best effort). */
  preserveUnreadCursor?: boolean;
  /** Split oversized Markdown into several messages (default true). */
  autoChunk?: boolean;
}

export interface SendResult {
  channel: string;
  ts: string;
  chunkTs: string[];
}

export interface UpdateOptions {
  channel: string;
  ts: string;
  markdown?: string;
  blocks?: unknown[];
}

export interface CreateStreamOptions {
  channel: string;
  threadTs?: string;
  initialMarkdown?: string;
  preserveUnreadCursor?: boolean;
}

export interface ChatSchedulerOptions {
  /** Cross-channel send concurrency; one slot is reserved for `interactive` (default 4). */
  globalSendConcurrency?: number;
  /** Hard deadline per send task (default 30000). */
  itemDeadlineMs?: number;
  /** Workspace-wide stream refresh rate shared by all streams (default 2/s, burst 4). */
  streamUpdatesPerSecond?: number;
  /** Minimum interval between refreshes of one stream (default 500ms). */
  streamMinIntervalMs?: number;
  /** Notice appended to unfinished streams on `client.stop()`. */
  interruptedNotice?: string;
}

/** Hooks implemented by the client (loop guard, unread-cursor guard, recovery bookkeeping). */
export interface ChatHooks {
  beforeAutoWrite(channel: string, threadTs: string | undefined): void;
  withUnreadGuard<T>(channel: string, enabled: boolean, fn: () => Promise<T>): Promise<T>;
  noteThreadActivity(channel: string, threadTs: string): void;
}

const CHAT_WINDOW_MS = 2_000;
const NOT_FOUND_RETRY_MS = 175;
const NOT_FOUND_RETRIES = 10;

function tsToMs(ts: string): number {
  return Number(ts.split(".")[0]) * 1000 + Number((ts.split(".")[1] ?? "0").padEnd(6, "0").slice(0, 3));
}

export class ChatApi {
  private readonly semaphore: PrioritySemaphore;
  private readonly bucket: TokenBucket;
  private readonly channelTails = new Map<string, Promise<unknown>>();
  private readonly echoes = new LruCache<string, string>(2000, 5 * 60_000);
  private readonly sentAt = new LruCache<string, number>(5000, 60_000);
  private readonly typingAt = new LruCache<string, number>(1000, 60_000);
  private readonly streams = new Set<StreamControllerImpl>();
  private readonly itemDeadlineMs: number;
  private readonly streamMinIntervalMs: number;
  readonly interruptedNotice: string;

  constructor(
    private readonly core: GabrielCore,
    private readonly registry: SelfRegistry,
    private readonly hooks: ChatHooks,
    private readonly logger: Logger,
    opts: ChatSchedulerOptions = {},
  ) {
    this.semaphore = new PrioritySemaphore(opts.globalSendConcurrency ?? 4, 1);
    const rate = opts.streamUpdatesPerSecond ?? 2;
    this.bucket = new TokenBucket(rate, Math.max(1, Math.ceil(rate * 2)));
    this.itemDeadlineMs = opts.itemDeadlineMs ?? 30_000;
    this.streamMinIntervalMs = opts.streamMinIntervalMs ?? 500;
    this.interruptedNotice = opts.interruptedNotice ?? "\n\n_(interrupted)_";
  }

  /** Records a WebSocket echo of a message the SDK sent (dual-channel confirmation). */
  noteEcho(clientMsgId: string, ts: string): void {
    this.echoes.set(clientMsgId, ts);
  }

  /** Called by the client for every message.deleted frame (cancels affected streams). */
  noteDeleted(ts: string): void {
    for (const s of this.streams) s.notifyDeleted(ts);
  }

  get activeStreams(): ReadonlySet<StreamControllerImpl> {
    return this.streams;
  }

  async send(opts: SendOptions, ctxSignal?: AbortSignal): Promise<SendResult> {
    const channel = assertChannelId(opts.channel);
    if (opts.threadTs !== undefined) assertTs(opts.threadTs, "threadTs");
    if ((opts.markdown === undefined) === (opts.blocks === undefined)) {
      throw new GabrielError("INVALID_ARGUMENT", "chat.send: pass exactly one of `markdown` or `blocks`");
    }
    if (opts.markdown !== undefined && (typeof opts.markdown !== "string" || opts.markdown.trim().length === 0)) {
      throw new GabrielError("INVALID_ARGUMENT", "chat.send: `markdown` must be a non-empty string");
    }
    if (ctxSignal?.aborted) throw new GabrielError("CANCELLED", "context cancelled before send");
    this.hooks.beforeAutoWrite(channel, opts.threadTs);
    const autoChunk = opts.autoChunk !== false;
    const pieces = opts.markdown !== undefined && autoChunk ? chunkMarkdown(opts.markdown) : [opts.markdown];
    const priority: SendPriority = opts.priority ?? "normal";

    const result = await this.hooks.withUnreadGuard(channel, opts.preserveUnreadCursor === true, () =>
      this.inChannelOrder(channel, async () => {
        const chunkTs: string[] = [];
        for (const piece of pieces) {
          const posted = await this.postWithResplit(
            { channel, ...(piece !== undefined ? { markdown: piece } : { blocks: opts.blocks! }), threadTs: opts.threadTs, broadcast: opts.broadcast },
            priority,
            autoChunk,
            DEFAULT_CHUNK_BUDGET,
            ctxSignal,
          );
          chunkTs.push(...posted);
        }
        return { channel, ts: chunkTs[0]!, chunkTs };
      }),
    );
    if (opts.threadTs) this.hooks.noteThreadActivity(channel, opts.threadTs);
    return result;
  }

  async update(opts: UpdateOptions): Promise<{ channel: string; ts: string }> {
    const channel = assertChannelId(opts.channel);
    const ts = assertTs(opts.ts);
    if ((opts.markdown === undefined) === (opts.blocks === undefined)) {
      throw new GabrielError("INVALID_ARGUMENT", "chat.update: pass exactly one of `markdown` or `blocks`");
    }
    await this.rawUpdate(channel, ts, opts.markdown, opts.blocks, "normal");
    return { channel, ts };
  }

  async delete(opts: { channel: string; ts: string }): Promise<void> {
    const channel = assertChannelId(opts.channel);
    const ts = assertTs(opts.ts);
    await this.withNotFoundRetry(ts, () => this.core.callWebApi("chat.delete", { channel, ts }, { reason: "delete_message" }));
  }

  /** Sends a typing indicator over the WebSocket, throttled to once per 3s per location. */
  async sendTyping(opts: { channel: string; threadTs?: string }): Promise<void> {
    const channel = assertChannelId(opts.channel);
    const key = `${channel}:${opts.threadTs ?? ""}`;
    const last = this.typingAt.get(key);
    if (last !== undefined && Date.now() - last < 3_000) return;
    this.typingAt.set(key, Date.now());
    try {
      this.core.sendWsFrame({ type: "user_typing", channel, ...(opts.threadTs ? { thread_ts: opts.threadTs } : {}) });
    } catch (err) {
      this.logger.debug({ err: String(err) }, "typing indicator skipped (socket not connected)");
    }
  }

  async createStream(opts: CreateStreamOptions, linkedSignal?: AbortSignal): Promise<StreamController> {
    const channel = assertChannelId(opts.channel);
    if (opts.threadTs !== undefined) assertTs(opts.threadTs, "threadTs");
    this.hooks.beforeAutoWrite(channel, opts.threadTs);
    const initial = opts.initialMarkdown && opts.initialMarkdown.trim() ? opts.initialMarkdown : "…";
    const post = (markdown: string, threadTs?: string) =>
      this.hooks.withUnreadGuard(channel, opts.preserveUnreadCursor === true, () =>
        this.inChannelOrder(channel, async () => {
          const [ts] = await this.postWithResplit({ channel, markdown, threadTs }, "interactive", false, DEFAULT_CHUNK_BUDGET, linkedSignal);
          return ts!;
        }),
      );
    const firstTs = await post(initial, opts.threadTs);
    if (opts.threadTs) this.hooks.noteThreadActivity(channel, opts.threadTs);
    const stream: StreamControllerImpl = new StreamControllerImpl(
      firstTs,
      {
        channel,
        ...(opts.threadTs ? { threadTs: opts.threadTs } : {}),
        budget: DEFAULT_CHUNK_BUDGET,
        minIntervalMs: this.streamMinIntervalMs,
        ...(linkedSignal ? { linkedSignal } : {}),
      },
      {
        post: (markdown, o) => post(markdown, o.threadTs),
        update: (ts, markdown) => this.rawUpdate(channel, ts, markdown, undefined, "stream_update"),
      },
      this.bucket,
      this.registry,
      this.logger,
      (s) => this.streams.delete(s),
    );
    this.streams.add(stream);
    return stream;
  }

  /** Interrupts all unfinished streams (graceful shutdown). */
  async interruptAll(): Promise<void> {
    await Promise.all([...this.streams].map((s) => s.interrupt(this.interruptedNotice)));
  }

  /** Waits for all queued channel sends to finish. */
  async drain(): Promise<void> {
    await Promise.allSettled([...this.channelTails.values()]);
  }

  private inChannelOrder<T>(channel: string, fn: () => Promise<T>): Promise<T> {
    const prev = this.channelTails.get(channel) ?? Promise.resolve();
    const run = prev.catch(() => {}).then(fn);
    const tail = run.catch(() => {});
    this.channelTails.set(channel, tail);
    void tail.then(() => {
      if (this.channelTails.get(channel) === tail) this.channelTails.delete(channel);
    });
    return run;
  }

  private async postWithResplit(
    msg: { channel: string; markdown?: string; blocks?: unknown[]; threadTs?: string | undefined; broadcast?: boolean | undefined },
    priority: SendPriority,
    autoChunk: boolean,
    budget: number,
    signal?: AbortSignal,
  ): Promise<string[]> {
    try {
      return [await this.postOne(msg, priority, signal)];
    } catch (err) {
      const code = (err as { code?: string }).code;
      if (autoChunk && msg.markdown !== undefined && (code === "MSG_TOO_LONG" || code === "MSG_BLOCKS_TOO_LONG") && budget > 500) {
        const next = Math.floor(budget * 0.8);
        const pieces = chunkMarkdown(msg.markdown, next, Math.min(DEFAULT_CHUNK_MIN, Math.floor(next * 0.7)));
        if (pieces.length === 1 && budget < 2000) throw err;
        const out: string[] = [];
        for (const p of pieces) out.push(...(await this.postWithResplit({ ...msg, markdown: p }, priority, autoChunk, next, signal)));
        return out;
      }
      throw err;
    }
  }

  private async postOne(
    msg: { channel: string; markdown?: string; blocks?: unknown[]; threadTs?: string | undefined; broadcast?: boolean | undefined },
    priority: SendPriority,
    signal?: AbortSignal,
  ): Promise<string> {
    const clientMsgId = randomUUID();
    this.registry.registerClientMsgId(clientMsgId);
    const deadline = AbortSignal.timeout(this.itemDeadlineMs);
    const combined = anySignal(deadline, signal);
    const release = await this.semaphore.acquire(priority, combined);
    try {
      const fields: Record<string, unknown> = {
        channel: msg.channel,
        type: "message",
        client_msg_id: clientMsgId,
        include_channel_perm_error: true,
        unfurl: "[]",
      };
      if (msg.markdown !== undefined) fields.markdown_text = msg.markdown;
      else fields.blocks = msg.blocks;
      if (msg.threadTs) {
        fields.thread_ts = msg.threadTs;
        if (msg.broadcast) fields.reply_broadcast = true;
      }
      let res: { ts?: string; message?: Record<string, unknown> };
      try {
        res = await this.core.callWebApi("chat.postMessage", fields, { reason: "webapp_message_send", signal: combined });
      } catch (err) {
        // Dual-channel confirmation: if the WebSocket already delivered our message, the send succeeded.
        const echoed = this.echoes.get(clientMsgId);
        const code = (err as { code?: string }).code;
        if (echoed && (code === "NETWORK_TIMEOUT" || code === "SERVER_ERROR" || code === "CANCELLED")) {
          this.registry.registerSentTs(echoed);
          return echoed;
        }
        if (deadline.aborted && !signal?.aborted) throw new GabrielError("NETWORK_TIMEOUT", "chat.postMessage exceeded itemDeadlineMs", { cause: err });
        throw err;
      }
      const ts = res.ts ?? (typeof res.message?.ts === "string" ? res.message.ts : undefined);
      if (!ts || !isTs(ts)) throw new GabrielError("SLACK_API_ERROR", "chat.postMessage returned no ts");
      this.registry.registerSentTs(ts);
      if (res.message) this.registry.registerWrittenHash(ts, contentHash(res.message));
      this.sentAt.set(ts, Date.now());
      return ts;
    } finally {
      release();
    }
  }

  private async rawUpdate(channel: string, ts: string, markdown: string | undefined, blocks: unknown[] | undefined, priority: SendPriority): Promise<void> {
    this.registry.registerPendingEdit(ts);
    const release = await this.semaphore.acquire(priority, AbortSignal.timeout(this.itemDeadlineMs));
    try {
      const fields: Record<string, unknown> = { channel, ts, skip_dlp_user_warning: false };
      if (markdown !== undefined) fields.markdown_text = markdown;
      else fields.blocks = blocks;
      const res = await this.withNotFoundRetry(ts, () =>
        this.core.callWebApi<{ message?: Record<string, unknown> }>("chat.update", fields, { reason: "saveMessageEdit" }),
      );
      if (res.message) this.registry.registerWrittenHash(ts, contentHash(res.message));
    } finally {
      release();
    }
  }

  /** Retries `message_not_found` every 175ms (max 10) only within 2s of the message's creation. */
  private async withNotFoundRetry<T>(ts: string, fn: () => Promise<T>): Promise<T> {
    const createdAt = this.sentAt.get(ts) ?? tsToMs(ts);
    for (let attempt = 0; ; attempt++) {
      try {
        return await fn();
      } catch (err) {
        const raw = (err as { rawSlackError?: string }).rawSlackError;
        if (raw !== "message_not_found") throw err;
        if (Date.now() - createdAt > CHAT_WINDOW_MS || attempt >= NOT_FOUND_RETRIES) throw err;
        await sleep(NOT_FOUND_RETRY_MS);
      }
    }
  }
}
