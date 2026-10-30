import { GabrielError } from "../errors.js";
import { chunk, deferred, type Deferred } from "../util/async.js";
import { LruCache } from "../util/lru.js";

export interface BatchLoaderOptions<V> {
  /** Loads a batch; ids missing from the returned map are treated as "not found". */
  batchFn(ids: string[]): Promise<Map<string, V>>;
  windowMs?: number;
  maxBatch?: number;
  concurrency?: number;
  ttlMs?: number;
  negativeTtlMs?: number;
  capacity?: number;
}

/**
 * DataLoader: coalesces lookups inside a short window into batched requests,
 * shares in-flight requests (singleflight), caches positives, and caches
 * "not found" results for 10 minutes. Transient errors are never cached.
 */
export class BatchLoader<V> {
  private readonly cache: LruCache<string, V | null>;
  private readonly inflight = new Map<string, Deferred<V | null>>();
  private queue: string[] = [];
  private timer: NodeJS.Timeout | undefined;
  private running = 0;
  private readonly pendingBatches: string[][] = [];
  private readonly windowMs: number;
  private readonly maxBatch: number;
  private readonly concurrency: number;
  private readonly ttlMs: number;
  private readonly negativeTtlMs: number;

  constructor(private readonly opts: BatchLoaderOptions<V>) {
    this.windowMs = opts.windowMs ?? 10;
    this.maxBatch = opts.maxBatch ?? 100;
    this.concurrency = opts.concurrency ?? 2;
    this.ttlMs = opts.ttlMs ?? 60 * 60_000;
    this.negativeTtlMs = opts.negativeTtlMs ?? 10 * 60_000;
    this.cache = new LruCache(opts.capacity ?? 20_000);
  }

  /** Synchronous cache read: value, `null` (known missing) or `undefined` (unknown). */
  peek(id: string): V | null | undefined {
    return this.cache.get(id);
  }

  prime(id: string, value: V): void {
    this.cache.set(id, value, this.ttlMs);
  }

  invalidate(id: string): void {
    this.cache.delete(id);
  }

  load(id: string, opts: { fresh?: boolean } = {}): Promise<V | null> {
    if (!opts.fresh) {
      const cached = this.cache.get(id);
      if (cached !== undefined) return Promise.resolve(cached);
    }
    const existing = this.inflight.get(id);
    if (existing) return existing.promise;
    const d = deferred<V | null>();
    this.inflight.set(id, d);
    this.queue.push(id);
    if (this.queue.length >= this.maxBatch) this.flush();
    else if (!this.timer) this.timer = setTimeout(() => this.flush(), this.windowMs);
    return d.promise;
  }

  loadMany(ids: string[]): Promise<Array<V | null>> {
    return Promise.all(ids.map((id) => this.load(id)));
  }

  private flush(): void {
    clearTimeout(this.timer);
    this.timer = undefined;
    const ids = this.queue;
    this.queue = [];
    for (const batch of chunk(ids, this.maxBatch)) this.pendingBatches.push(batch);
    this.pump();
  }

  private pump(): void {
    while (this.running < this.concurrency && this.pendingBatches.length > 0) {
      const batch = this.pendingBatches.shift()!;
      this.running++;
      void this.runBatch(batch).finally(() => {
        this.running--;
        this.pump();
      });
    }
  }

  private async runBatch(ids: string[]): Promise<void> {
    try {
      const found = await this.opts.batchFn(ids);
      for (const id of ids) {
        const value = found.get(id);
        if (value === undefined) this.cache.set(id, null, this.negativeTtlMs);
        else this.cache.set(id, value, this.ttlMs);
        this.settle(id, (d) => d.resolve(value ?? null));
      }
    } catch (err) {
      const error = err instanceof GabrielError ? err : new GabrielError("SERVER_ERROR", "entity lookup failed", { cause: err });
      for (const id of ids) this.settle(id, (d) => d.reject(error));
    }
  }

  private settle(id: string, fn: (d: Deferred<V | null>) => void): void {
    const d = this.inflight.get(id);
    if (!d) return;
    this.inflight.delete(id);
    fn(d);
  }
}
