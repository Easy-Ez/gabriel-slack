/**
 * Bounded keyed executor (spec/behavior.md §6.3): global concurrency + per-key
 * concurrency + bounded queue. Jobs can temporarily give up their permits
 * (`suspend`) while awaiting a reply and re-acquire them ahead of queued work
 * (`resume`, head-of-line priority), which prevents re-entrancy deadlocks.
 */
export interface JobHandle {
  /** Releases the key + global permits (e.g. while waiting for a reply). */
  suspend(): void;
  /** Re-acquires permits with priority over queued jobs. */
  resume(): Promise<void>;
  /** Releases permits for good without waiting for the job (handler-timeout grace). */
  forceRelease(): void;
}

interface Job<M> {
  key: string;
  meta: M;
  run: (handle: JobHandle) => Promise<void>;
}

interface Resumer {
  key: string;
  grant: () => void;
}

export interface KeyedExecutorOptions {
  globalConcurrency: number;
  perKeyConcurrency: number;
  maxQueue: number;
}

export class KeyedExecutor<M> {
  private readonly queues = new Map<string, Job<M>[]>();
  private readonly running = new Map<string, number>();
  private globalRunning = 0;
  private queued = 0;
  private readonly resumers: Resumer[] = [];
  private active = 0;
  private idleWaiters: Array<() => void> = [];

  constructor(private readonly opts: KeyedExecutorOptions) {}

  get queueDepth(): number {
    return this.queued;
  }

  get activeJobs(): number {
    return this.active;
  }

  /** Enqueues a job; returns false when the queue is full (caller handles overflow). */
  submit(key: string, meta: M, run: (handle: JobHandle) => Promise<void>): boolean {
    if (this.queued >= this.opts.maxQueue) return false;
    let q = this.queues.get(key);
    if (!q) this.queues.set(key, (q = []));
    q.push({ key, meta, run });
    this.queued++;
    this.pump();
    return true;
  }

  /** Removes and returns the first queued job for `key` whose meta matches. */
  extractPending(key: string, predicate: (meta: M) => boolean): M | undefined {
    const q = this.queues.get(key);
    if (!q) return undefined;
    const idx = q.findIndex((j) => predicate(j.meta));
    if (idx < 0) return undefined;
    const [job] = q.splice(idx, 1);
    this.queued--;
    if (q.length === 0) this.queues.delete(key);
    return job!.meta;
  }

  /** Resolves when no job is queued or running. */
  idle(): Promise<void> {
    if (this.active === 0 && this.queued === 0) return Promise.resolve();
    return new Promise((resolve) => this.idleWaiters.push(resolve));
  }

  private canRun(key: string): boolean {
    return this.globalRunning < this.opts.globalConcurrency && (this.running.get(key) ?? 0) < this.opts.perKeyConcurrency;
  }

  private acquire(key: string): void {
    this.globalRunning++;
    this.running.set(key, (this.running.get(key) ?? 0) + 1);
  }

  private release(key: string): void {
    this.globalRunning--;
    const n = (this.running.get(key) ?? 1) - 1;
    if (n <= 0) this.running.delete(key);
    else this.running.set(key, n);
  }

  private pump(): void {
    // Resuming jobs first (head-of-line priority).
    for (let i = 0; i < this.resumers.length; ) {
      const r = this.resumers[i]!;
      if (this.canRun(r.key)) {
        this.resumers.splice(i, 1);
        this.acquire(r.key);
        r.grant();
      } else i++;
    }
    for (const [key, q] of this.queues) {
      if (this.globalRunning >= this.opts.globalConcurrency) break;
      while (q.length > 0 && this.canRun(key) && !this.resumers.some((r) => r.key === key)) {
        const job = q.shift()!;
        this.queued--;
        this.start(job);
      }
      if (q.length === 0) this.queues.delete(key);
    }
  }

  private start(job: Job<M>): void {
    this.acquire(job.key);
    this.active++;
    let held = true;
    let done = false;
    let resuming: Promise<void> | undefined;
    const handle: JobHandle = {
      suspend: () => {
        if (!held || done) return;
        held = false;
        this.release(job.key);
        this.pump();
      },
      resume: () => {
        if (held || done) return Promise.resolve();
        if (resuming) return resuming;
        resuming = new Promise<void>((resolve) => {
          this.resumers.push({
            key: job.key,
            grant: () => {
              resuming = undefined;
              if (done) {
                // The job finished (or was force-released) while waiting: give the permit back.
                this.release(job.key);
                this.pump();
              } else held = true;
              resolve();
            },
          });
          this.pump();
        });
        return resuming;
      },
      forceRelease: () => {
        if (done) return;
        done = true;
        if (held) {
          held = false;
          this.release(job.key);
        }
        this.finishJob();
      },
    };
    void job
      .run(handle)
      .catch(() => {})
      .finally(() => {
        if (done) return;
        done = true;
        if (held) {
          held = false;
          this.release(job.key);
        }
        this.finishJob();
      });
  }

  private finishJob(): void {
    this.active--;
    this.pump();
    if (this.active === 0 && this.queued === 0) {
      const waiters = this.idleWaiters;
      this.idleWaiters = [];
      for (const w of waiters) w();
    }
  }
}
