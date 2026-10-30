import { compareTs } from "../util/validate.js";

/**
 * Pluggable persistence for recovery watermarks, the watched-thread list and
 * (in future multi-replica stores) consumption locks. Values are opaque strings.
 */
export interface StateStore {
  get(key: string): Promise<string | undefined>;
  set(key: string, value: string, opts?: { ttlMs?: number }): Promise<void>;
  delete(key: string): Promise<void>;
  /** Sets the key only when absent. Returns true when this call created it (lock primitive). */
  setIfAbsent(key: string, value: string, opts?: { ttlMs?: number }): Promise<boolean>;
  /**
   * Monotonic max-merge for Slack timestamps: stores `ts` only if it is newer than
   * the current value. Returns the value now stored.
   */
  maxMergeTs(key: string, ts: string): Promise<string>;
  /** Lists keys with the given prefix. */
  keys(prefix: string): Promise<string[]>;
  /** Flushes pending writes and releases resources. */
  close(): Promise<void>;
  /** true when state survives process restarts (enables restart backfill). */
  readonly persistent: boolean;
}

interface Entry {
  value: string;
  expiresAt?: number;
}

/** In-memory store (default). State is lost on restart. */
export class MemoryStateStore implements StateStore {
  readonly persistent: boolean = false;
  protected readonly data = new Map<string, Entry>();

  constructor(protected readonly now: () => number = Date.now) {}

  protected live(key: string): Entry | undefined {
    const e = this.data.get(key);
    if (e && e.expiresAt !== undefined && e.expiresAt <= this.now()) {
      this.data.delete(key);
      return undefined;
    }
    return e;
  }

  async get(key: string): Promise<string | undefined> {
    return this.live(key)?.value;
  }

  async set(key: string, value: string, opts: { ttlMs?: number } = {}): Promise<void> {
    this.data.set(key, opts.ttlMs === undefined ? { value } : { value, expiresAt: this.now() + opts.ttlMs });
    this.changed();
  }

  async delete(key: string): Promise<void> {
    this.data.delete(key);
    this.changed();
  }

  async setIfAbsent(key: string, value: string, opts: { ttlMs?: number } = {}): Promise<boolean> {
    if (this.live(key)) return false;
    await this.set(key, value, opts);
    return true;
  }

  async maxMergeTs(key: string, ts: string): Promise<string> {
    const cur = this.live(key)?.value;
    if (cur !== undefined && compareTs(cur, ts) >= 0) return cur;
    await this.set(key, ts);
    return ts;
  }

  async keys(prefix: string): Promise<string[]> {
    return [...this.data.keys()].filter((k) => k.startsWith(prefix) && this.live(k) !== undefined);
  }

  async close(): Promise<void> {}

  /** Hook for persistent subclasses. */
  protected changed(): void {}
}
