import { cancelledError } from "../errors.js";

export type SendPriority = "interactive" | "normal" | "stream_update";

const RANK: Record<SendPriority, number> = { interactive: 0, normal: 1, stream_update: 2 };

interface Waiter {
  priority: SendPriority;
  seq: number;
  grant: () => void;
}

/**
 * Global send semaphore with three priorities. `reserved` slots are only usable by
 * `interactive` work, so direct replies still go out during an alert storm.
 */
export class PrioritySemaphore {
  private inUse = 0;
  private seq = 0;
  private readonly waiters: Waiter[] = [];

  constructor(
    private readonly total: number,
    private readonly reserved = 1,
  ) {
    if (total < 1) throw new RangeError("semaphore total must be >= 1");
  }

  private allowed(priority: SendPriority): boolean {
    const limit = priority === "interactive" ? this.total : Math.max(1, this.total - this.reserved);
    return this.inUse < limit;
  }

  acquire(priority: SendPriority, signal?: AbortSignal): Promise<() => void> {
    if (signal?.aborted) return Promise.reject(cancelledError(signal.reason));
    return new Promise((resolve, reject) => {
      const waiter: Waiter = {
        priority,
        seq: this.seq++,
        grant: () => {
          signal?.removeEventListener("abort", onAbort);
          let released = false;
          resolve(() => {
            if (released) return;
            released = true;
            this.inUse--;
            this.pump();
          });
        },
      };
      const onAbort = () => {
        const idx = this.waiters.indexOf(waiter);
        if (idx >= 0) this.waiters.splice(idx, 1);
        reject(cancelledError(signal?.reason));
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      this.waiters.push(waiter);
      this.waiters.sort((a, b) => RANK[a.priority] - RANK[b.priority] || a.seq - b.seq);
      this.pump();
    });
  }

  private pump(): void {
    for (let i = 0; i < this.waiters.length; ) {
      const w = this.waiters[i]!;
      if (this.allowed(w.priority)) {
        this.waiters.splice(i, 1);
        this.inUse++;
        w.grant();
      } else i++;
    }
  }
}

/** Token bucket shared by all concurrent streams (round-robin fairness via FIFO waiters). */
export class TokenBucket {
  private tokens: number;
  private last: number;
  private readonly waiters: Array<() => void> = [];
  private timer: NodeJS.Timeout | undefined;

  constructor(
    private readonly ratePerSec: number,
    private readonly burst: number,
    private readonly now: () => number = Date.now,
  ) {
    this.tokens = burst;
    this.last = now();
  }

  take(signal?: AbortSignal): Promise<void> {
    if (signal?.aborted) return Promise.reject(cancelledError(signal.reason));
    return new Promise((resolve, reject) => {
      const grant = () => {
        signal?.removeEventListener("abort", onAbort);
        resolve();
      };
      const onAbort = () => {
        const idx = this.waiters.indexOf(grant);
        if (idx >= 0) this.waiters.splice(idx, 1);
        reject(cancelledError(signal?.reason));
      };
      signal?.addEventListener("abort", onAbort, { once: true });
      this.waiters.push(grant);
      this.pump();
    });
  }

  private refill(): void {
    const t = this.now();
    this.tokens = Math.min(this.burst, this.tokens + ((t - this.last) / 1000) * this.ratePerSec);
    this.last = t;
  }

  private pump(): void {
    this.refill();
    while (this.waiters.length > 0 && this.tokens >= 1) {
      this.tokens -= 1;
      this.waiters.shift()!();
    }
    if (this.waiters.length > 0 && !this.timer) {
      const waitMs = Math.ceil(((1 - this.tokens) / this.ratePerSec) * 1000);
      this.timer = setTimeout(() => {
        this.timer = undefined;
        this.pump();
      }, Math.max(1, waitMs));
    }
  }
}
