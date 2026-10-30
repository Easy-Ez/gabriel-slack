import type { MessageOrigin } from "../events/types.js";
import { LruCache } from "../util/lru.js";

const SELF_TTL_MS = 5 * 60_000;
const EDIT_ECHO_WINDOW_MS = 15_000;

/**
 * Self-send registry (spec/behavior.md §8). Writes are registered *before* the network
 * request so a fast WebSocket echo can never beat the bookkeeping.
 */
export class SelfRegistry {
  private readonly clientMsgIds = new LruCache<string, true>(2000, SELF_TTL_MS);
  private readonly sentTs = new LruCache<string, true>(2000, SELF_TTL_MS);
  /** ts → content hashes the SDK wrote recently. */
  private readonly writtenHashes = new LruCache<string, string[]>(2000, SELF_TTL_MS);
  /** ts → edit echo window deadline. */
  private readonly pendingEdits = new LruCache<string, number>(2000, SELF_TTL_MS);
  private readonly activeStreams = new Set<string>();
  /** channel → ts values the SDK itself marked as read (conversations.mark echoes). */
  private readonly selfMarks = new LruCache<string, Set<string>>(1000, 60_000);

  constructor(
    private readonly selfUserId: () => string,
    private readonly now: () => number = Date.now,
  ) {}

  registerClientMsgId(id: string): void {
    this.clientMsgIds.set(id, true);
  }

  registerSentTs(ts: string): void {
    this.sentTs.set(ts, true);
  }

  /** Called before chat.update is sent. */
  registerPendingEdit(ts: string): void {
    this.pendingEdits.set(ts, this.now() + EDIT_ECHO_WINDOW_MS);
  }

  registerWrittenHash(ts: string, hash: string): void {
    const list = this.writtenHashes.get(ts) ?? [];
    list.push(hash);
    if (list.length > 20) list.shift();
    this.writtenHashes.set(ts, list);
  }

  beginStream(ts: string): void {
    this.activeStreams.add(ts);
  }

  endStream(ts: string): void {
    this.activeStreams.delete(ts);
  }

  isActiveStream(ts: string): boolean {
    return this.activeStreams.has(ts);
  }

  isSelfSent(ts: string): boolean {
    return this.sentTs.has(ts) || this.activeStreams.has(ts);
  }

  registerSelfMark(channel: string, ts: string): void {
    const set = this.selfMarks.get(channel) ?? new Set<string>();
    set.add(ts);
    this.selfMarks.set(channel, set);
  }

  isSelfMark(channel: string, ts: string): boolean {
    return this.selfMarks.get(channel)?.has(ts) === true || this.sentTs.has(ts);
  }

  classify(info: { user?: string; botId?: string; clientMsgId?: string; ts: string; contentHash: string; isEdit: boolean }): MessageOrigin {
    if (info.botId !== undefined) return "bot";
    if (info.user === undefined || info.user !== this.selfUserId()) return "other_user";
    const known =
      (info.clientMsgId !== undefined && this.clientMsgIds.has(info.clientMsgId)) || this.sentTs.has(info.ts) || this.activeStreams.has(info.ts);
    if (!known) return "self_other_client";
    if (!info.isEdit) return "this_client";
    if (this.activeStreams.has(info.ts)) return "this_client";
    if (this.writtenHashes.get(info.ts)?.includes(info.contentHash)) return "this_client";
    const deadline = this.pendingEdits.get(info.ts);
    if (deadline !== undefined && deadline >= this.now()) return "this_client";
    // The account owner edited a message the SDK had sent.
    return "self_other_client";
  }
}

/** Sliding-window auto-reply circuit breaker per (channel, thread). */
export class LoopBreaker {
  private readonly windows = new Map<string, number[]>();

  constructor(
    private readonly maxPerMinute: number,
    private readonly now: () => number = Date.now,
  ) {}

  /** Records an automatic write; returns the window count if it would exceed the limit. */
  tryRecord(channel: string, threadTs: string | undefined): { allowed: boolean; count: number } {
    const key = `${channel}:${threadTs ?? "-"}`;
    const cutoff = this.now() - 60_000;
    const list = (this.windows.get(key) ?? []).filter((t) => t > cutoff);
    if (list.length >= this.maxPerMinute) {
      this.windows.set(key, list);
      return { allowed: false, count: list.length };
    }
    list.push(this.now());
    this.windows.set(key, list);
    if (this.windows.size > 5000) {
      for (const [k, v] of this.windows) if (v.every((t) => t <= cutoff)) this.windows.delete(k);
    }
    return { allowed: true, count: list.length };
  }
}
