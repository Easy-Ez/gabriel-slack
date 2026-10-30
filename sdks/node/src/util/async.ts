import { cancelledError } from "../errors.js";

/** Resolves after `ms`, rejecting with CANCELLED as soon as `signal` aborts. */
export function sleep(ms: number, signal?: AbortSignal): Promise<void> {
  if (signal?.aborted) return Promise.reject(cancelledError(signal.reason));
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, Math.max(0, ms));
    const onAbort = () => {
      clearTimeout(timer);
      reject(cancelledError(signal?.reason));
    };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

export interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

export function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Collapses concurrent calls with the same key into one in-flight promise. */
export class Singleflight<T> {
  private readonly inflight = new Map<string, Promise<T>>();

  do(key: string, fn: () => Promise<T>): Promise<T> {
    const existing = this.inflight.get(key);
    if (existing) return existing;
    const p = fn().finally(() => this.inflight.delete(key));
    this.inflight.set(key, p);
    return p;
  }
}

/** Full-jitter exponential backoff: random(0, min(cap, base * 2^attempt)). */
export function fullJitterDelay(attempt: number, baseMs: number, capMs: number, random: () => number = Math.random): number {
  const ceiling = Math.min(capMs, baseMs * 2 ** Math.max(0, attempt));
  return Math.floor(random() * ceiling);
}

/** Splits an array into fixed-size chunks. */
export function chunk<T>(items: readonly T[], size: number): T[][] {
  const out: T[][] = [];
  for (let i = 0; i < items.length; i += size) out.push(items.slice(i, i + size));
  return out;
}

/** Combines optional signals into one (`AbortSignal.any` with undefined filtering). */
export function anySignal(...signals: Array<AbortSignal | undefined>): AbortSignal {
  const real = signals.filter((s): s is AbortSignal => s !== undefined);
  if (real.length === 1) return real[0]!;
  return AbortSignal.any(real);
}

/** Resolves `promise`, or rejects with `onTimeout()` after `ms`. */
export function withTimeout<T>(promise: Promise<T>, ms: number, onTimeout: () => unknown): Promise<T> {
  let timer: NodeJS.Timeout | undefined;
  const timeout = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(onTimeout()), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}
