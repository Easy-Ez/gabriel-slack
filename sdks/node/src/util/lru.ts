/** Minimal LRU map with optional per-entry TTL. */
export class LruCache<K, V> {
  private readonly map = new Map<K, { value: V; expiresAt: number }>();

  constructor(
    private readonly capacity: number,
    private readonly defaultTtlMs = Number.POSITIVE_INFINITY,
    private readonly now: () => number = Date.now,
  ) {}

  get size(): number {
    return this.map.size;
  }

  get(key: K): V | undefined {
    const entry = this.map.get(key);
    if (!entry) return undefined;
    if (entry.expiresAt <= this.now()) {
      this.map.delete(key);
      return undefined;
    }
    this.map.delete(key);
    this.map.set(key, entry);
    return entry.value;
  }

  has(key: K): boolean {
    return this.get(key) !== undefined;
  }

  set(key: K, value: V, ttlMs = this.defaultTtlMs): void {
    this.map.delete(key);
    this.map.set(key, { value, expiresAt: this.now() + ttlMs });
    while (this.map.size > this.capacity) {
      const oldest = this.map.keys().next();
      if (oldest.done) break;
      this.map.delete(oldest.value);
    }
  }

  delete(key: K): boolean {
    return this.map.delete(key);
  }

  clear(): void {
    this.map.clear();
  }

  keys(): K[] {
    return [...this.map.keys()];
  }

  /** Inserts the key and reports whether it was already present (dedupe helper). */
  seen(key: K, value: V): boolean {
    if (this.has(key)) return true;
    this.set(key, value);
    return false;
  }
}
