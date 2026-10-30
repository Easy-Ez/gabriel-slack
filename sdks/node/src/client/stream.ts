import { GabrielError } from "../errors.js";
import { chunkMarkdown, healOpenFence, measureMarkdown } from "../markdown/chunker.js";
import { sleep } from "../util/async.js";
import type { Logger } from "../util/logger.js";
import type { TokenBucket } from "../util/semaphore.js";
import type { SelfRegistry } from "./origin.js";

export interface StreamController {
  /** ts of the first (placeholder) message. */
  readonly ts: string;
  /** Appends text. Synchronous O(1): only the in-memory buffer changes. */
  append(chunk: string): void;
  /** Flushes the final content and closes the stream. */
  finish(): Promise<{ ts: string; continuationTs: string[] }>;
  /** Stops refreshing and closes the stream. Already-posted messages stay. */
  abort(reason?: string): Promise<void>;
  /** Aborted when the streamed message is deleted in Slack (or the linked ctx.signal fires). */
  readonly signal: AbortSignal;
}

export interface StreamTransport {
  post(markdown: string, opts: { threadTs?: string }): Promise<string>;
  update(ts: string, markdown: string): Promise<void>;
}

export interface StreamOptions {
  channel: string;
  threadTs?: string;
  budget: number;
  minIntervalMs: number;
  linkedSignal?: AbortSignal;
}

const ECHO_GRACE_MS = 15_000;

/**
 * Single in-flight, latest-wins streaming controller (spec/behavior.md §9).
 * `append()` bumps a dirty generation; at most one `chat.update` per stream is in
 * flight; when it returns and newer content exists, the newest snapshot is sent.
 * The final message content equals the buffer at `finish()`.
 */
export class StreamControllerImpl implements StreamController {
  readonly ts: string;
  private readonly controller = new AbortController();
  private current = "";
  private currentTs: string;
  private readonly continuationTs: string[] = [];
  private dirtyGen = 0;
  private sentGen = 0;
  private inflight: Promise<void> | undefined;
  private closed = false;
  private lastFlushAt = 0;
  private budget: number;
  private readonly allTs: string[];

  constructor(
    firstTs: string,
    private readonly opts: StreamOptions,
    private readonly transport: StreamTransport,
    private readonly bucket: TokenBucket,
    private readonly registry: SelfRegistry,
    private readonly logger: Logger,
    private readonly onClosed: (stream: StreamControllerImpl) => void,
  ) {
    this.ts = firstTs;
    this.currentTs = firstTs;
    this.allTs = [firstTs];
    this.budget = opts.budget;
    registry.beginStream(firstTs);
    opts.linkedSignal?.addEventListener("abort", () => this.cancel(opts.linkedSignal?.reason ?? "cancelled"), { once: true });
  }

  get signal(): AbortSignal {
    return this.controller.signal;
  }

  get messageTs(): readonly string[] {
    return this.allTs;
  }

  append(chunk: string): void {
    if (this.closed) throw new GabrielError("STREAM_CLOSED", "stream already finished or aborted");
    if (typeof chunk !== "string" || chunk.length === 0) return;
    this.current += chunk;
    this.dirtyGen++;
    this.schedule();
  }

  async finish(): Promise<{ ts: string; continuationTs: string[] }> {
    if (this.closed && this.controller.signal.aborted) {
      throw new GabrielError("STREAM_CLOSED", "stream was aborted");
    }
    this.closed = true;
    await this.drain(true);
    this.release();
    return { ts: this.ts, continuationTs: [...this.continuationTs] };
  }

  async abort(reason = "aborted"): Promise<void> {
    if (this.closed && this.controller.signal.aborted) return;
    this.closed = true;
    this.controller.abort(reason);
    await this.inflight?.catch(() => {});
    this.release();
  }

  /** Graceful shutdown: append a notice and flush what we have. */
  async interrupt(notice: string): Promise<void> {
    if (this.closed) return;
    this.current += notice;
    this.dirtyGen++;
    await this.finish().catch((err) => this.logger.warn({ err: String(err) }, "stream interrupt flush failed"));
  }

  /** Called by the client when any of this stream's messages is deleted. */
  notifyDeleted(ts: string): void {
    if (this.allTs.includes(ts)) this.cancel("message_deleted");
  }

  private cancel(reason: unknown): void {
    if (this.controller.signal.aborted) return;
    this.closed = true;
    this.controller.abort(reason);
    this.release();
  }

  private release(): void {
    const ts = [...this.allTs];
    // Keep echo suppression alive briefly for late edit echoes.
    setTimeout(() => ts.forEach((t) => this.registry.endStream(t)), ECHO_GRACE_MS).unref?.();
    this.onClosed(this);
  }

  private schedule(): void {
    if (this.inflight || this.controller.signal.aborted) return;
    if (this.dirtyGen <= this.sentGen) return;
    this.inflight = this.flushOnce(false).finally(() => {
      this.inflight = undefined;
      if (!this.closed) this.schedule();
    });
  }

  private async drain(final: boolean): Promise<void> {
    for (let guard = 0; guard < 100; guard++) {
      if (this.inflight) await this.inflight.catch(() => {});
      if (this.controller.signal.aborted) return;
      if (this.dirtyGen <= this.sentGen) return;
      this.inflight = this.flushOnce(final).finally(() => {
        this.inflight = undefined;
      });
    }
  }

  private async flushOnce(final: boolean): Promise<void> {
    const signal = this.controller.signal;
    try {
      if (!final) {
        const wait = this.lastFlushAt + this.opts.minIntervalMs - Date.now();
        if (wait > 0) await sleep(wait, signal);
      }
      await this.bucket.take(signal);
      const gen = this.dirtyGen;
      if (measureMarkdown(this.current).effective > this.budget) {
        await this.rollover();
      }
      const body = this.current.length > 0 ? this.current : "…";
      await this.transport.update(this.currentTs, final ? body : healOpenFence(body));
      this.lastFlushAt = Date.now();
      if (gen > this.sentGen) this.sentGen = gen;
    } catch (err) {
      if (signal.aborted) return;
      const code = (err as { code?: string }).code;
      const raw = (err as { rawSlackError?: string }).rawSlackError;
      if (code === "MSG_TOO_LONG" || code === "MSG_BLOCKS_TOO_LONG") {
        this.budget = Math.floor(this.budget * 0.8);
        return;
      }
      if (raw === "message_not_found") {
        this.cancel("message_deleted");
        return;
      }
      this.logger.warn({ err: String(err), code }, "stream refresh failed; will retry with the latest content");
      await sleep(1_000, signal).catch(() => {});
      if (final) throw err;
    }
  }

  /** Finalizes the current message with the head of the buffer and continues in a new message. */
  private async rollover(): Promise<void> {
    // `append()` may run while we await; only the prefix we split is replaced.
    let base = this.current.length;
    const pieces = chunkMarkdown(this.current, this.budget, Math.floor(this.budget * 0.7));
    while (pieces.length > 1) {
      const head = pieces.shift()!;
      await this.transport.update(this.currentTs, head);
      const next = pieces[0]!;
      const newTs = await this.transport.post(healOpenFence(next.length > 0 ? next : "…"), this.opts.threadTs ? { threadTs: this.opts.threadTs } : {});
      this.registry.beginStream(newTs);
      this.allTs.push(newTs);
      this.continuationTs.push(newTs);
      this.currentTs = newTs;
      // Trim after every successful hand-over so a later failure never re-sends finalized text.
      const appended = this.current.slice(base);
      const rest = pieces.join("\n\n");
      this.current = rest + appended;
      base = rest.length;
    }
  }
}
