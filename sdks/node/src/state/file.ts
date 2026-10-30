import { mkdirSync, readFileSync, renameSync, writeFileSync, chmodSync, existsSync, statSync } from "node:fs";
import path from "node:path";
import { GabrielError } from "../errors.js";
import { MemoryStateStore } from "./store.js";

export interface FileStateStoreOptions {
  /** Directory holding `state.json`. Created with 0700 permissions when missing. */
  dir: string;
  /** Write debounce (default 1000ms). */
  flushIntervalMs?: number;
}

/**
 * Single-host persistent store: an in-memory map snapshotted to `{dir}/state.json`
 * (atomic temp-file rename, 0600 permissions). Enables restart backfill from
 * persisted watermarks. Not safe for multiple processes sharing one directory.
 */
export class FileStateStore extends MemoryStateStore {
  override readonly persistent = true;
  private readonly file: string;
  private readonly flushIntervalMs: number;
  private timer: NodeJS.Timeout | undefined;
  private dirty = false;

  constructor(opts: FileStateStoreOptions) {
    super();
    if (typeof opts.dir !== "string" || opts.dir.length === 0 || opts.dir.includes("\0")) {
      throw new GabrielError("INVALID_ARGUMENT", "FileStateStore.dir must be a non-empty path");
    }
    const dir = path.resolve(opts.dir);
    mkdirSync(dir, { recursive: true, mode: 0o700 });
    if (!statSync(dir).isDirectory()) throw new GabrielError("INVALID_ARGUMENT", "FileStateStore.dir is not a directory");
    this.file = path.join(dir, "state.json");
    this.flushIntervalMs = opts.flushIntervalMs ?? 1000;
    this.load();
  }

  private load(): void {
    if (!existsSync(this.file)) return;
    try {
      const parsed = JSON.parse(readFileSync(this.file, "utf8")) as Record<string, { value: string; expiresAt?: number }>;
      for (const [k, e] of Object.entries(parsed)) {
        if (e && typeof e.value === "string") this.data.set(k, e.expiresAt === undefined ? { value: e.value } : { value: e.value, expiresAt: e.expiresAt });
      }
    } catch (err) {
      throw new GabrielError("STATE_STORE_UNAVAILABLE", `cannot read ${this.file}`, { cause: err });
    }
  }

  protected override changed(): void {
    this.dirty = true;
    if (this.timer) return;
    this.timer = setTimeout(() => {
      this.timer = undefined;
      this.flushSync();
    }, this.flushIntervalMs);
    this.timer.unref?.();
  }

  /** Writes the snapshot now (atomic rename). */
  flushSync(): void {
    if (!this.dirty) return;
    const snapshot: Record<string, { value: string; expiresAt?: number }> = {};
    const now = this.now();
    for (const [k, e] of this.data) if (e.expiresAt === undefined || e.expiresAt > now) snapshot[k] = e;
    const tmp = `${this.file}.${process.pid}.tmp`;
    writeFileSync(tmp, JSON.stringify(snapshot), { mode: 0o600 });
    chmodSync(tmp, 0o600);
    renameSync(tmp, this.file);
    this.dirty = false;
  }

  override async close(): Promise<void> {
    clearTimeout(this.timer);
    this.timer = undefined;
    this.flushSync();
  }
}
