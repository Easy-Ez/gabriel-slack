import type { RawWsFrame } from "../core/types.js";
import type { GabrielCore } from "../core/core.js";
import type { ConnectionGapEvent } from "../events/types.js";
import type { StateStore } from "../state/store.js";
import { GabrielError } from "../errors.js";
import type { Logger } from "../util/logger.js";
import { compareTs, isTs, maxTs } from "../util/validate.js";
import type { ChannelsApi } from "./apis.js";

interface Lane {
  inflight: Set<string>;
  pinned: Set<string>;
  completed: Set<string>;
  wm?: string;
  dirty: boolean;
}

function minOf(sets: Array<Set<string>>): string | undefined {
  let min: string | undefined;
  for (const set of sets) for (const ts of set) if (min === undefined || compareTs(ts, min) < 0) min = ts;
  return min;
}

/**
 * Sliding low-watermarks (spec/behavior.md §10): advanced only by `message.created`,
 * held back by in-flight and overflow-pinned events, persisted with max-merge.
 */
export class WatermarkTracker {
  private readonly lanes = new Map<string, Lane>();
  /** Activity list: threads this client wrote in or subscribed to (bounded LRU). */
  private readonly watchedThreads = new Map<string, { channel: string; threadTs: string }>();
  /** Explicit list: local-only `threads.watch()`; never evicted by the LRU. */
  private readonly explicitThreads = new Map<string, { channel: string; threadTs: string }>();
  /** Thread watermark keys to delete from the store on the next flush. */
  private readonly droppedThreadMarks = new Set<string>();
  private flushTimer: NodeJS.Timeout | undefined;

  constructor(
    private readonly store: StateStore,
    private readonly teamId: () => string,
    private readonly watchedThreadsLimit: number,
    private readonly logger: Logger,
    private readonly maxExplicitThreadWatches = 500,
  ) {}

  private lane(key: string): Lane {
    let l = this.lanes.get(key);
    if (!l) this.lanes.set(key, (l = { inflight: new Set(), pinned: new Set(), completed: new Set(), dirty: false }));
    return l;
  }

  /**
   * `threadOnly`: replies fetched directly from a thread (not part of channel history) move only
   * the thread's lane, so they never start or advance channel-history recovery.
   */
  private keysFor(channel: string, threadTs?: string, threadOnly = false): string[] {
    const keys = threadOnly ? [] : [`c:${channel}`];
    if (threadTs && this.isThreadWatched(channel, threadTs)) keys.push(`t:${channel}:${threadTs}`);
    return keys;
  }

  begin(channel: string, ts: string, threadTs?: string, threadOnly = false): void {
    for (const k of this.keysFor(channel, threadTs, threadOnly)) this.lane(k).inflight.add(ts);
  }

  complete(channel: string, ts: string, threadTs?: string, threadOnly = false): void {
    for (const k of this.keysFor(channel, threadTs, threadOnly)) {
      const l = this.lane(k);
      l.inflight.delete(ts);
      l.completed.add(ts);
      this.advance(l);
    }
    this.scheduleFlush();
  }

  /** Overflow: keeps the watermark below `ts` until a gap recovery re-delivers it. */
  pin(channel: string, ts: string, threadTs?: string, threadOnly = false): void {
    for (const k of this.keysFor(channel, threadTs, threadOnly)) {
      const l = this.lane(k);
      l.inflight.delete(ts);
      l.pinned.add(ts);
    }
  }

  /** Clears overflow pins of a channel and of its watched threads (after a recovery refetched them). */
  unpinAll(channel: string): void {
    for (const [key, l] of this.lanes) {
      if (key !== `c:${channel}` && !key.startsWith(`t:${channel}:`)) continue;
      if (l.pinned.size === 0) continue;
      l.pinned.clear();
      this.advance(l);
    }
  }

  hasPinned(): boolean {
    for (const l of this.lanes.values()) if (l.pinned.size > 0) return true;
    return false;
  }

  private advance(l: Lane): void {
    const minPending = minOf([l.inflight, l.pinned]);
    let best: string | undefined;
    for (const ts of [...l.completed]) {
      if (minPending === undefined || compareTs(ts, minPending) < 0) {
        best = maxTs(best, ts);
        l.completed.delete(ts);
      }
    }
    if (best !== undefined && (l.wm === undefined || compareTs(best, l.wm) > 0)) {
      l.wm = best;
      l.dirty = true;
    }
  }

  channelWatermarks(): Map<string, string> {
    const out = new Map<string, string>();
    for (const [k, l] of this.lanes) {
      if (!k.startsWith("c:")) continue;
      // Pinned (dropped) events force recovery from just before the earliest drop.
      const pinnedMin = minOf([l.pinned]);
      const wm = l.wm;
      if (wm !== undefined) out.set(k.slice(2), pinnedMin !== undefined && compareTs(pinnedMin, wm) <= 0 ? pinnedMin : wm);
    }
    return out;
  }

  threadWatermarks(): Array<{ channel: string; threadTs: string; wm: string }> {
    const out: Array<{ channel: string; threadTs: string; wm: string }> = [];
    const seen = new Set<string>();
    for (const list of [this.explicitThreads, this.watchedThreads]) {
      for (const [key, { channel, threadTs }] of list) {
        if (seen.has(key)) continue;
        seen.add(key);
        out.push({ channel, threadTs, wm: this.threadWatermark(channel, threadTs) });
      }
    }
    return out;
  }

  threadWatermark(channel: string, threadTs: string): string {
    return this.lanes.get(`t:${channel}:${threadTs}`)?.wm ?? threadTs;
  }

  /** Adds a thread to the bounded LRU of threads whose replies are recovered directly. */
  watchThread(channel: string, threadTs: string): void {
    const key = `${channel}:${threadTs}`;
    this.watchedThreads.delete(key);
    this.watchedThreads.set(key, { channel, threadTs });
    while (this.watchedThreads.size > this.watchedThreadsLimit) {
      const oldest = this.watchedThreads.keys().next().value as string;
      this.watchedThreads.delete(oldest);
      this.dropThreadLane(oldest);
    }
    this.scheduleFlush();
  }

  unwatchThread(channel: string, threadTs: string): void {
    const key = `${channel}:${threadTs}`;
    this.watchedThreads.delete(key);
    this.dropThreadLane(key);
    this.scheduleFlush();
  }

  /**
   * Adds a thread to the explicit (local-only) list. Its watermark becomes the newer of the
   * current one and `after` (default: the root). Throws when a new thread exceeds the bound.
   */
  watchExplicit(channel: string, threadTs: string, after?: string): void {
    const key = `${channel}:${threadTs}`;
    if (!this.explicitThreads.has(key) && this.explicitThreads.size >= this.maxExplicitThreadWatches) {
      throw new GabrielError(
        "SUBSCRIPTION_CAP_EXCEEDED",
        `explicit thread watches would reach ${this.explicitThreads.size + 1} (> ${this.maxExplicitThreadWatches})`,
      );
    }
    this.explicitThreads.set(key, { channel, threadTs });
    const lane = this.lane(`t:${key}`);
    const initial = maxTs(threadTs, after) as string;
    if (lane.wm === undefined || compareTs(initial, lane.wm) > 0) {
      lane.wm = initial;
      lane.dirty = true;
    }
    this.scheduleFlush();
  }

  unwatchExplicit(channel: string, threadTs: string): void {
    const key = `${channel}:${threadTs}`;
    if (!this.explicitThreads.delete(key)) return;
    this.dropThreadLane(key);
    this.scheduleFlush();
  }

  explicitWatches(): Array<{ channel: string; threadTs: string; watermark: string }> {
    return [...this.explicitThreads.values()].map(({ channel, threadTs }) => ({ channel, threadTs, watermark: this.threadWatermark(channel, threadTs) }));
  }

  isExplicitlyWatched(channel: string, threadTs: string): boolean {
    return this.explicitThreads.has(`${channel}:${threadTs}`);
  }

  isThreadWatched(channel: string, threadTs: string): boolean {
    const key = `${channel}:${threadTs}`;
    return this.watchedThreads.has(key) || this.explicitThreads.has(key);
  }

  /** Clears overflow pins of one thread lane after its replies were refetched. */
  unpinThread(channel: string, threadTs: string): void {
    const l = this.lanes.get(`t:${channel}:${threadTs}`);
    if (!l || l.pinned.size === 0) return;
    l.pinned.clear();
    this.advance(l);
  }

  /** Forgets a thread's lane and stored watermark once neither list holds the thread. */
  private dropThreadLane(key: string): void {
    if (this.watchedThreads.has(key) || this.explicitThreads.has(key)) return;
    this.lanes.delete(`t:${key}`);
    this.droppedThreadMarks.add(key);
  }

  async load(): Promise<void> {
    const prefix = `gabriel:wm:${this.teamId()}:`;
    for (const key of await this.store.keys(prefix)) {
      const value = await this.store.get(key);
      if (!value || !isTs(value)) continue;
      const lane = this.lane(key.slice(prefix.length));
      lane.wm = maxTs(lane.wm, value);
    }
    const raw = await this.store.get(`gabriel:threads:${this.teamId()}`);
    if (raw) {
      try {
        for (const t of JSON.parse(raw) as Array<{ channel: string; threadTs: string }>) {
          if (typeof t.channel === "string" && isTs(t.threadTs)) this.watchedThreads.set(`${t.channel}:${t.threadTs}`, { channel: t.channel, threadTs: t.threadTs });
        }
      } catch {
        this.logger.warn({}, "ignoring corrupt watched-thread list");
      }
    }
    const explicit = await this.store.get(`gabriel:watches:${this.teamId()}`);
    if (explicit) {
      try {
        for (const t of JSON.parse(explicit) as Array<{ channel: string; threadTs: string }>) {
          if (typeof t.channel === "string" && isTs(t.threadTs)) this.explicitThreads.set(`${t.channel}:${t.threadTs}`, { channel: t.channel, threadTs: t.threadTs });
        }
      } catch {
        this.logger.warn({}, "ignoring corrupt explicit thread-watch list");
      }
    }
  }

  private scheduleFlush(): void {
    if (this.flushTimer) return;
    this.flushTimer = setTimeout(() => {
      this.flushTimer = undefined;
      void this.flush().catch((err) => this.logger.warn({ err: String(err) }, "watermark flush failed"));
    }, 1_000);
    this.flushTimer.unref?.();
  }

  async flush(): Promise<void> {
    clearTimeout(this.flushTimer);
    this.flushTimer = undefined;
    const team = this.teamId();
    // Deletions first: a thread dropped and watched again starts from its new watermark.
    const dropped = [...this.droppedThreadMarks];
    this.droppedThreadMarks.clear();
    await Promise.all(dropped.map((key) => this.store.delete(`gabriel:wm:${team}:t:${key}`)));
    const writes: Array<Promise<unknown>> = [];
    for (const [k, l] of this.lanes) {
      if (!l.dirty || l.wm === undefined) continue;
      l.dirty = false;
      writes.push(this.store.maxMergeTs(`gabriel:wm:${team}:${k}`, l.wm));
    }
    writes.push(this.store.set(`gabriel:threads:${team}`, JSON.stringify([...this.watchedThreads.values()])));
    writes.push(this.store.set(`gabriel:watches:${team}`, JSON.stringify([...this.explicitThreads.values()])));
    await Promise.all(writes);
  }
}

export interface ThreadFetchResult {
  /** Replies newer than the watermark, as raw message frames (root excluded). */
  frames: RawWsFrame[];
  /** Why the fetch stopped early; frames fetched before that are still returned. */
  failure?: "page_limit" | "rate_limited" | "api_error";
}

const reasonOf = (err: unknown) => ((err as { code?: string }).code === "RATE_LIMITED" ? "rate_limited" : "api_error") as "rate_limited" | "api_error";

/** `conversations.replies(oldest = wm, inclusive = false)`, bounded pages. Never throws. */
export async function fetchThreadReplies(
  core: GabrielCore,
  t: { channel: string; threadTs: string; wm: string },
  maxPages: number,
): Promise<ThreadFetchResult> {
  const frames: RawWsFrame[] = [];
  let cursor: string | undefined;
  let pages = 0;
  try {
    do {
      const res = await core.callWebApi<{ messages?: Array<Record<string, unknown>>; has_more?: boolean; response_metadata?: { next_cursor?: string } }>(
        "conversations.replies",
        { channel: t.channel, ts: t.threadTs, oldest: t.wm, inclusive: false, limit: 100, include_tombstones: true, ...(cursor ? { cursor } : {}) },
        { reason: "history-api/fetchReplies" },
      );
      pages++;
      for (const m of res.messages ?? []) {
        if (typeof m.ts !== "string" || m.ts === t.threadTs || compareTs(m.ts, t.wm) <= 0) continue;
        frames.push({ ...m, type: "message", channel: t.channel });
      }
      cursor = res.has_more ? res.response_metadata?.next_cursor || undefined : undefined;
      if (cursor && pages >= maxPages) return { frames, failure: "page_limit" };
    } while (cursor);
    return { frames };
  } catch (err) {
    return { frames, failure: reasonOf(err) };
  }
}

export interface RecoveryResult {
  frames: RawWsFrame[];
  gap: Omit<ConnectionGapEvent, "type" | "eventId" | "cause">;
}

/**
 * Gap recovery: `client.counts` → per-channel `conversations.history(oldest=wm)`
 * paged towards older messages, plus direct `conversations.replies` for watched
 * threads (plain thread replies never appear in channel history).
 */
export async function recoverGap(
  core: GabrielCore,
  channels: ChannelsApi,
  marks: WatermarkTracker,
  opts: { maxPagesPerChannel: number; logger: Logger },
): Promise<RecoveryResult> {
  const frames: RawWsFrame[] = [];
  const gap: RecoveryResult["gap"] = {
    recovered: true,
    recoveredCount: 0,
    channelsRecovered: [],
    threadsRecovered: [],
    truncated: false,
    unrecovered: [],
  };
  const wms = marks.channelWatermarks();
  if (wms.size === 0 && marks.threadWatermarks().length === 0) return { frames, gap };

  let latestByChannel = new Map<string, string>();
  try {
    const counts = await core.callWebApi<Record<string, Array<{ id?: string; latest?: string }>>>("client.counts", {}, { reason: "gabriel-gap-recovery" });
    for (const group of ["channels", "mpims", "ims"]) {
      for (const c of counts[group] ?? []) if (c.id && c.latest && isTs(c.latest)) latestByChannel.set(c.id, c.latest);
    }
  } catch (err) {
    opts.logger.warn({ err: String(err) }, "client.counts failed; probing every tracked channel");
    latestByChannel = new Map();
  }

  let minFrom: string | undefined;
  let maxTo: string | undefined;

  for (const [channel, wm] of wms) {
    const latest = latestByChannel.get(channel);
    if (latestByChannel.size > 0 && (latest === undefined || compareTs(latest, wm) <= 0)) continue;
    let cursor: string | undefined;
    let pages = 0;
    let got = 0;
    try {
      do {
        const page = await channels.rawHistory({ channel, oldest: wm, inclusive: false, limit: 100, ...(cursor ? { cursor } : {}) });
        pages++;
        for (const m of page.messages) {
          if (typeof m.ts !== "string" || compareTs(m.ts, wm) <= 0) continue;
          frames.push({ ...m, type: "message", channel });
          got++;
          maxTo = maxTs(maxTo, m.ts);
        }
        cursor = page.hasMore ? page.nextCursor : undefined;
        if (cursor && pages >= opts.maxPagesPerChannel) {
          gap.truncated = true;
          gap.unrecovered.push({ channel, fromTs: wm, reason: "page_limit" });
          break;
        }
      } while (cursor);
      if (got > 0) gap.channelsRecovered.push(channel);
      minFrom = minFrom === undefined || compareTs(wm, minFrom) < 0 ? wm : minFrom;
    } catch (err) {
      gap.unrecovered.push({ channel, fromTs: wm, reason: reasonOf(err) });
    }
  }

  for (const t of marks.threadWatermarks()) {
    const res = await fetchThreadReplies(core, t, opts.maxPagesPerChannel);
    frames.push(...res.frames);
    if (res.frames.length > 0 && (!res.failure || res.failure === "page_limit")) gap.threadsRecovered.push({ channel: t.channel, threadTs: t.threadTs });
    if (res.failure) {
      if (res.failure === "page_limit") gap.truncated = true;
      gap.unrecovered.push({ channel: t.channel, threadTs: t.threadTs, fromTs: t.wm, reason: res.failure });
    }
  }

  frames.sort((a, b) => compareTs(String(a.ts), String(b.ts)));
  gap.recoveredCount = frames.length;
  gap.recovered = gap.unrecovered.length === 0;
  if (minFrom) gap.fromTs = minFrom;
  if (maxTo) gap.toTs = maxTo;
  return { frames, gap };
}
