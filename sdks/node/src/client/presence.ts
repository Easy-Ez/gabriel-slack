import type { GabrielCore } from "../core/core.js";
import type { RawWsFrame } from "../core/types.js";
import { GabrielError } from "../errors.js";
import type { DndInfo, PresenceChangedEvent, UserDndChangedEvent, UserStatus, UserStatusChangedEvent } from "../events/types.js";
import { chunk } from "../util/async.js";
import type { Logger } from "../util/logger.js";
import { assertUserId } from "../util/validate.js";
import { toUserRecord, type Hydrator } from "./hydrator.js";

/** Serialized `presence_sub` frames above this many bytes are silently dropped by the gateway (16 KiB). */
export const PRESENCE_FRAME_MAX_BYTES = 16_000;
export const PROFILE_POOL_CAP = 900;
const PROFILE_FRAME_BATCH = 100;
const PRESENCE_DEBOUNCE_MS = 100;
const SNAPSHOT_TIMEOUT_MS = 15_000;
const GATEWAY_ID_CAP = 12;
const GATEWAY_RATE_LIMIT = 15;

export interface SubscriptionHandle extends AsyncDisposable {
  readonly userIds: readonly string[];
  unsubscribe(): Promise<void>;
}

export interface PresenceStatus {
  presence: "active" | "away" | "unknown";
  status?: UserStatus;
  dnd?: DndInfo;
}

interface PresenceEntry {
  presence: "active" | "away" | "unknown";
  epoch: number;
  updatedAt: number;
}

type Emit = (
  evt: PresenceChangedEvent | UserStatusChangedEvent | UserDndChangedEvent,
  laneKey: string,
) => void;

/** Byte length of the presence_sub frame for a set of ids (id field included). */
export function presenceFrameBytes(ids: Iterable<string>): number {
  return Buffer.byteLength(JSON.stringify({ type: "presence_sub", ids: [...ids], id: 2_147_483_647 }));
}

/**
 * Two independently-capped subscription pools with single-writer reconcilers
 * (spec/behavior.md §7):
 * - presence pool: full-overwrite `presence_sub`, no ACK, debounced, <= 16,000 bytes/frame;
 * - profile/DND pool: incremental `subscription_update`, 100 ids/frame, ACKed, <= 900 ids.
 * Desired sets are ref-counted; every `hello` starts a new epoch and replays both pools.
 */
export class PresenceApi {
  private readonly presenceRefs = new Map<string, number>();
  private readonly profileRefs = new Map<string, number>();
  private presenceApplied = new Set<string>();
  private profileApplied = new Set<string>();
  private readonly cache = new Map<string, PresenceEntry>();
  private readonly lastDnd = new Map<string, DndInfo>();
  private epoch = 0;
  private presenceTimer: NodeJS.Timeout | undefined;
  private profileRunning: Promise<void> | undefined;
  private profileDirty = false;
  private profileFull = false;
  private seq = 0;

  constructor(
    private readonly core: GabrielCore,
    private readonly hydrator: Hydrator,
    private readonly emit: Emit,
    private readonly logger: Logger,
  ) {}

  /** Watch users' presence (and optionally profile status + DND). Ref-counted. */
  async watch(userIds: string[], opts: { trackProfileAndDnd?: boolean } = {}): Promise<SubscriptionHandle> {
    if (!Array.isArray(userIds) || userIds.length === 0) throw new GabrielError("INVALID_ARGUMENT", "presence.watch: userIds must be a non-empty array");
    const ids = [...new Set(userIds.map((u) => assertUserId(u)))];
    const track = opts.trackProfileAndDnd === true;

    const nextPresence = new Set([...this.presenceRefs.keys(), ...ids]);
    if (presenceFrameBytes(nextPresence) > PRESENCE_FRAME_MAX_BYTES) {
      throw new GabrielError(
        "SUBSCRIPTION_CAP_EXCEEDED",
        `presence subscription would serialize to ${presenceFrameBytes(nextPresence)} bytes (> ${PRESENCE_FRAME_MAX_BYTES}); about 1,150 standard user IDs fit`,
      );
    }
    if (track) {
      const nextProfile = new Set([...this.profileRefs.keys(), ...ids]);
      if (nextProfile.size > PROFILE_POOL_CAP) {
        throw new GabrielError("SUBSCRIPTION_CAP_EXCEEDED", `profile/DND subscriptions would reach ${nextProfile.size} users (> ${PROFILE_POOL_CAP})`);
      }
    }

    const newlyPresence: string[] = [];
    for (const id of ids) {
      const n = this.presenceRefs.get(id) ?? 0;
      this.presenceRefs.set(id, n + 1);
      if (n === 0) {
        newlyPresence.push(id);
        // A stale entry from an unwatched period must not suppress the initial snapshot.
        if (this.cache.get(id)?.epoch !== this.epoch) this.cache.delete(id);
      }
    }
    if (track) for (const id of ids) this.profileRefs.set(id, (this.profileRefs.get(id) ?? 0) + 1);

    // Users already snapshotted on this connection: the gateway will not re-push, so synthesize.
    queueMicrotask(() => {
      for (const id of ids) {
        const entry = this.cache.get(id);
        if (entry && entry.epoch === this.epoch && entry.presence !== "unknown") {
          this.emitPresence(id, entry.presence, "unknown", true);
        }
      }
    });
    if (newlyPresence.length > 0) this.schedulePresence();
    if (track) this.scheduleProfile();

    let released = false;
    const handle: SubscriptionHandle = {
      userIds: Object.freeze([...ids]),
      unsubscribe: async () => {
        if (released) return;
        released = true;
        let presenceChanged = false;
        for (const id of ids) {
          const n = (this.presenceRefs.get(id) ?? 1) - 1;
          if (n <= 0) {
            this.presenceRefs.delete(id);
            presenceChanged = true;
          } else this.presenceRefs.set(id, n);
          if (track) {
            const p = (this.profileRefs.get(id) ?? 1) - 1;
            if (p <= 0) this.profileRefs.delete(id);
            else this.profileRefs.set(id, p);
          }
        }
        if (presenceChanged) this.schedulePresence();
        if (track) this.scheduleProfile();
      },
      [Symbol.asyncDispose]: () => handle.unsubscribe(),
    };
    return handle;
  }

  /** Cached presence + profile status + DND for a user; fetches what is missing. */
  async getStatus(userId: string): Promise<PresenceStatus> {
    const id = assertUserId(userId);
    const presence = this.cache.get(id)?.presence ?? "unknown";
    const [user, dnd] = await Promise.all([
      this.hydrator.users.load(id).catch(() => null),
      this.lastDnd.has(id) ? Promise.resolve(this.lastDnd.get(id)) : this.hydrator.fetchDnd([id]).then((m) => m[id]).catch(() => undefined),
    ]);
    if (dnd) this.lastDnd.set(id, dnd);
    return { presence, ...(user ? { status: user.status } : {}), ...(dnd ? { dnd } : {}) };
  }

  get watchedUserIds(): string[] {
    return [...this.presenceRefs.keys()];
  }

  isProfileTracked(userId: string): boolean {
    return this.profileRefs.has(userId);
  }

  /** New connection epoch: the gateway kept nothing, so replay both pools. */
  onHello(epoch: number): void {
    this.epoch = epoch;
    this.presenceApplied = new Set();
    this.profileApplied = new Set();
    this.profileFull = false;
    if (this.presenceRefs.size > 0) this.flushPresence();
    if (this.profileRefs.size > 0) this.scheduleProfile();
  }

  /** Handles presence-domain frames. Returns true when consumed. */
  handleFrame(frame: RawWsFrame): boolean {
    switch (frame.type) {
      case "presence_change": {
        const presence = frame.presence === "active" ? "active" : "away";
        const users = Array.isArray(frame.users) ? (frame.users as unknown[]) : typeof frame.user === "string" ? [frame.user] : [];
        for (const u of users) if (typeof u === "string") this.applyPresence(u, presence);
        return true;
      }
      case "manual_presence_change": {
        const self = this.core.session.userId;
        this.applyPresence(self, frame.presence === "active" ? "active" : "away");
        return true;
      }
      case "user_change":
      case "user_invalidated":
      case "user_status_changed": {
        const user = frame.user;
        const id = typeof user === "string" ? user : user && typeof user === "object" ? (user as { id?: string }).id : undefined;
        if (id && this.profileRefs.has(id)) void this.refreshProfile(id, user && typeof user === "object" && "profile" in user ? (user as Record<string, unknown>) : undefined);
        return true;
      }
      case "dnd_updated":
      case "dnd_updated_user":
      case "dnd_invalidated": {
        const user = frame.user;
        const id = typeof user === "string" ? user : user && typeof user === "object" ? (user as { id?: string }).id : this.core.session.userId;
        if (id && this.profileRefs.has(id)) void this.refreshDnd(id);
        return true;
      }
    }
    return false;
  }

  private applyPresence(userId: string, presence: "active" | "away"): void {
    const prev = this.cache.get(userId);
    this.cache.set(userId, { presence, epoch: this.epoch, updatedAt: Date.now() });
    if (!this.presenceRefs.has(userId)) return;
    if (!prev || prev.presence === "unknown") {
      this.emitPresence(userId, presence, prev?.presence ?? "unknown", true);
      return;
    }
    if (prev.presence === presence) return; // reconnect snapshot of an unchanged user
    this.emitPresence(userId, presence, prev.presence, false);
  }

  private emitPresence(userId: string, presence: "active" | "away", previous: PresenceEntry["presence"], initial: boolean): void {
    const user = this.hydrator.users.peek(userId);
    this.emit(
      {
        type: "presence.changed",
        eventId: `presence:${userId}:${this.epoch}:${++this.seq}`,
        user: { id: userId, ...(user?.displayName ? { displayName: user.displayName } : {}), ...(user?.handle ? { handle: user.handle } : {}) },
        presence,
        previousPresence: previous,
        isInitialSnapshot: initial,
      },
      userId,
    );
  }

  private schedulePresence(): void {
    clearTimeout(this.presenceTimer);
    this.presenceTimer = setTimeout(() => this.flushPresence(), PRESENCE_DEBOUNCE_MS);
  }

  /** Sends the full desired set (overwrite semantics, fire-and-forget). */
  private flushPresence(): void {
    clearTimeout(this.presenceTimer);
    this.presenceTimer = undefined;
    const desired = new Set(this.presenceRefs.keys());
    const same = desired.size === this.presenceApplied.size && [...desired].every((id) => this.presenceApplied.has(id));
    if (same) return;
    if (!this.core.ws.isConnected()) return; // replayed on next hello
    const ids = [...desired];
    if (presenceFrameBytes(ids) > PRESENCE_FRAME_MAX_BYTES) {
      this.logger.error({ count: ids.length }, "presence_sub exceeds frame cap; not sent");
      return;
    }
    try {
      this.core.sendWsFrame({ type: "presence_sub", ids });
      const epoch = this.epoch;
      const fresh = ids.filter((id) => !this.presenceApplied.has(id));
      this.presenceApplied = desired;
      setTimeout(() => {
        if (epoch !== this.epoch) return;
        for (const id of fresh) {
          const e = this.cache.get(id);
          if (!e || e.epoch !== epoch) this.cache.set(id, { presence: "unknown", epoch, updatedAt: Date.now() });
        }
      }, SNAPSHOT_TIMEOUT_MS).unref?.();
    } catch (err) {
      this.logger.debug({ err: String(err) }, "presence_sub deferred until reconnect");
    }
  }

  private scheduleProfile(): void {
    this.profileDirty = true;
    if (this.profileRunning) return;
    this.profileRunning = (async () => {
      while (this.profileDirty) {
        this.profileDirty = false;
        await this.reconcileProfile();
      }
    })()
      .catch((err) => this.logger.warn({ err: String(err) }, "profile subscription reconcile failed"))
      .finally(() => {
        this.profileRunning = undefined;
      });
  }

  /** Incremental diff, 100 ids per frame, each frame ACKed before `applied` advances. */
  private async reconcileProfile(): Promise<void> {
    if (!this.core.ws.isConnected()) return;
    const desired = new Set(this.profileRefs.keys());
    const add = [...desired].filter((id) => !this.profileApplied.has(id));
    const remove = [...this.profileApplied].filter((id) => !desired.has(id));
    const epoch = this.epoch;
    for (const batch of chunk(remove, PROFILE_FRAME_BATCH)) {
      await this.sendSubscription("user_change_unsubscribe_request", batch);
      await this.sendSubscription("dnd_invalidated_unsubscribe_request", batch);
      if (epoch !== this.epoch) return;
      for (const id of batch) this.profileApplied.delete(id);
    }
    if (this.profileFull) return;
    for (const batch of chunk(add, PROFILE_FRAME_BATCH)) {
      const ok = await this.sendSubscription("user_change_subscribe_request", batch);
      if (!ok) return;
      await this.sendSubscription("dnd_invalidated_subscribe_request", batch);
      if (epoch !== this.epoch) return;
      for (const id of batch) this.profileApplied.add(id);
      // Prime caches so the first invalidation can be diffed against a baseline.
      void this.hydrator.users.loadMany(batch).catch(() => {});
      void this.hydrator.fetchDnd(batch).then((m) => Object.entries(m).forEach(([k, v]) => this.lastDnd.set(k, v))).catch(() => {});
    }
  }

  private async sendSubscription(subtype: string, ids: string[], retried = false): Promise<boolean> {
    try {
      await this.core.sendWsCommand({ type: "subscription_update", subtype, ids });
      return true;
    } catch (err) {
      const frame = (err as { cause?: unknown }).cause as { error?: { gatewayErrorCode?: number; gateway_error_code?: number } } | undefined;
      const gw = frame?.error?.gatewayErrorCode ?? frame?.error?.gateway_error_code;
      if (gw === GATEWAY_ID_CAP) {
        this.profileFull = true;
        this.logger.warn({ subtype }, "gateway refused profile subscriptions: per-connection cap reached");
        return false;
      }
      if (gw === GATEWAY_RATE_LIMIT && !retried) {
        await new Promise((r) => setTimeout(r, 10_000));
        return this.sendSubscription(subtype, ids, true);
      }
      if ((err as { code?: string }).code === "NETWORK_TIMEOUT") {
        // Some subscription subtypes are not ACKed; treat as applied and move on.
        this.logger.debug({ subtype }, "subscription_update not acknowledged; assuming applied");
        return true;
      }
      throw err;
    }
  }

  private async refreshProfile(userId: string, inline?: Record<string, unknown>): Promise<void> {
    const before = this.hydrator.users.peek(userId);
    try {
      let after = inline && inline.profile ? undefined : await this.hydrator.users.load(userId, { fresh: true });
      if (inline && inline.profile) {
        after = toUserRecord({ id: userId, ...inline }, this.core.session.teamId);
        this.hydrator.users.prime(userId, after);
      }
      if (!after) return;
      const prev = before?.status;
      const cur = after.status;
      if (prev && prev.emoji === cur.emoji && prev.text === cur.text && prev.expiration === cur.expiration) return;
      this.emit(
        {
          type: "user.status_changed",
          eventId: `user_status:${userId}:${++this.seq}:${Date.now()}`,
          user: { id: userId, ...(after.displayName ? { displayName: after.displayName } : {}), ...(after.handle ? { handle: after.handle } : {}) },
          currentStatus: cur,
          ...(prev ? { previousStatus: prev } : {}),
        },
        userId,
      );
    } catch (err) {
      this.logger.debug({ err: String(err), userId }, "profile refetch failed");
    }
  }

  private async refreshDnd(userId: string): Promise<void> {
    try {
      const map = await this.hydrator.fetchDnd([userId]);
      const cur = map[userId];
      if (!cur) return;
      const prev = this.lastDnd.get(userId);
      this.lastDnd.set(userId, cur);
      if (prev && prev.enabled === cur.enabled && prev.nextStartTs === cur.nextStartTs && prev.nextEndTs === cur.nextEndTs) return;
      const user = this.hydrator.users.peek(userId);
      this.emit(
        {
          type: "user.dnd_changed",
          eventId: `user_dnd:${userId}:${++this.seq}:${Date.now()}`,
          user: { id: userId, ...(user?.displayName ? { displayName: user.displayName } : {}), ...(user?.handle ? { handle: user.handle } : {}) },
          dnd: cur,
          ...(prev ? { previousDnd: prev } : {}),
        },
        userId,
      );
    } catch (err) {
      this.logger.debug({ err: String(err), userId }, "dnd refetch failed");
    }
  }

  stop(): void {
    clearTimeout(this.presenceTimer);
  }
}
