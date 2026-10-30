import type { RawWsFrame } from "../core/types.js";
import type { GabrielCore } from "../core/core.js";
import type { ConnectionGapEvent } from "../events/types.js";
import type { StateStore } from "../state/store.js";
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
  private readonly watchedThreads = new Map<string, { channel: string; threadTs: string }>();
  private flushTimer: NodeJS.Timeout | undefined;

  constructor(
    private readonly store: StateStore,
    private readonly teamId: () => string,
    private readonly watchedThreadsLimit: number,
    private readonly logger: Logger,
  ) {}

  private lane(key: string): Lane {
    let l = this.lanes.get(key);
    if (!l) this.lanes.set(key, (l = { inflight: new Set(), pinned: new Set(), completed: new Set(), dirty: false }));
    return l;
  }

  private keysFor(channel: string, threadTs?: string): string[] {
    const keys = [`c:${channel}`];
    if (threadTs && this.watchedThreads.has(`${channel}:${threadTs}`)) keys.push(`t:${channel}:${threadTs}`);
    return keys;
  }

  begin(channel: string, ts: string, threadTs?: string): void {
    for (const k of this.keysFor(channel, threadTs)) this.lane(k).inflight.add(ts);
  }

  complete(channel: string, ts: string, threadTs?: string): void {
    for (const k of this.keysFor(channel, threadTs)) {
      const l = this.lane(k);
      l.inflight.delete(ts);
      l.completed.add(ts);
      this.advance(l);
    }
    this.scheduleFlush();
  }

  /** Overflow: keeps the watermark below `ts` until a gap recovery re-delivers it. */
  pin(channel: string, ts: string, threadTs?: string): void {
    for (const k of this.keysFor(channel, threadTs)) {
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
    for (const { channel, threadTs } of this.watchedThreads.values()) {
      const l = this.lanes.get(`t:${channel}:${threadTs}`);
      out.push({ channel, threadTs, wm: l?.wm ?? threadTs });
    }
    return out;
  }

  /** Adds a thread to the bounded LRU of threads whose replies are recovered directly. */
  watchThread(channel: string, threadTs: string): void {
    const key = `${channel}:${threadTs}`;
    this.watchedThreads.delete(key);
    this.watchedThreads.set(key, { channel, threadTs });
    while (this.watchedThreads.size > this.watchedThreadsLimit) {
      const oldest = this.watchedThreads.keys().next().value as string;
      this.watchedThreads.delete(oldest);
      this.lanes.delete(`t:${oldest}`);
    }
    this.scheduleFlush();
  }

  unwatchThread(channel: string, threadTs: string): void {
    this.watchedThreads.delete(`${channel}:${threadTs}`);
    this.lanes.delete(`t:${channel}:${threadTs}`);
    this.scheduleFlush();
  }

  isThreadWatched(channel: string, threadTs: string): boolean {
    return this.watchedThreads.has(`${channel}:${threadTs}`);
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
          if (typeof t.channel === "string" && isTs(t.threadTs)) this.watchedThreads.set(`${t.channel}:${t.threadTs}`, t);
        }
      } catch {
        this.logger.warn({}, "ignoring corrupt watched-thread list");
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
    const writes: Array<Promise<unknown>> = [];
    for (const [k, l] of this.lanes) {
      if (!l.dirty || l.wm === undefined) continue;
      l.dirty = false;
      writes.push(this.store.maxMergeTs(`gabriel:wm:${team}:${k}`, l.wm));
    }
    writes.push(this.store.set(`gabriel:threads:${team}`, JSON.stringify([...this.watchedThreads.values()])));
    await Promise.all(writes);
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

  const reasonOf = (err: unknown) => ((err as { code?: string }).code === "RATE_LIMITED" ? "rate_limited" : "api_error") as "rate_limited" | "api_error";
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
    let cursor: string | undefined;
    let pages = 0;
    let got = 0;
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
          got++;
        }
        cursor = res.has_more ? res.response_metadata?.next_cursor || undefined : undefined;
        if (cursor && pages >= opts.maxPagesPerChannel) {
          gap.truncated = true;
          gap.unrecovered.push({ channel: t.channel, threadTs: t.threadTs, fromTs: t.wm, reason: "page_limit" });
          break;
        }
      } while (cursor);
      if (got > 0) gap.threadsRecovered.push({ channel: t.channel, threadTs: t.threadTs });
    } catch (err) {
      gap.unrecovered.push({ channel: t.channel, threadTs: t.threadTs, fromTs: t.wm, reason: reasonOf(err) });
    }
  }

  frames.sort((a, b) => compareTs(String(a.ts), String(b.ts)));
  gap.recoveredCount = frames.length;
  gap.recovered = gap.unrecovered.length === 0;
  if (minFrom) gap.fromTs = minFrom;
  if (maxTo) gap.toTs = maxTo;
  return { frames, gap };
}
