import type { GabrielCore } from "../core/core.js";
import type { Draft } from "../events/normalize.js";
import type { ChannelRef, DndInfo, ReactionEvent, UserRef, UserStatus } from "../events/types.js";
import type { EntityResolver } from "../markdown/blocks-to-markdown.js";
import type { Logger } from "../util/logger.js";
import { LruCache } from "../util/lru.js";
import { chunk } from "../util/async.js";
import { BatchLoader } from "./loader.js";

/** Cached user record (superset of the public `UserRef`). */
export interface UserRecord extends UserRef {
  realName?: string;
  teamId?: string;
  deleted?: boolean;
  status: UserStatus;
}

export interface ChannelRecord extends ChannelRef {
  /** Peer user of a 1:1 DM. */
  imUserId?: string;
}

export interface UsergroupRecord {
  id: string;
  handle: string;
  name?: string;
  users?: string[];
}

type Obj = Record<string, unknown>;
const s = (v: unknown): string | undefined => (typeof v === "string" && v.length > 0 ? v : undefined);
const o = (v: unknown): Obj => (v && typeof v === "object" ? (v as Obj) : {});

export function toUserRecord(raw: Obj, selfTeamId: string): UserRecord {
  const profile = o(raw.profile);
  const name = s(raw.name);
  const realName = s(raw.real_name) ?? s(profile.real_name);
  const displayName = s(profile.display_name) ?? realName ?? name;
  const teamId = s(raw.team_id) ?? s(profile.team);
  const rec: UserRecord = {
    id: String(raw.id),
    isBot: raw.is_bot === true,
    status: {
      emoji: s(profile.status_emoji) ?? "",
      text: s(profile.status_text) ?? "",
      expiration: typeof profile.status_expiration === "number" ? profile.status_expiration : 0,
    },
  };
  if (name) {
    rec.name = name;
    rec.handle = name;
  }
  if (displayName) rec.displayName = displayName;
  if (realName) rec.realName = realName;
  if (teamId) rec.teamId = teamId;
  // Enterprise-grid colleagues share the enterprise; stranger / foreign-team users are external.
  if (raw.is_stranger === true || (teamId !== undefined && teamId !== selfTeamId && !raw.enterprise_user)) rec.isExternal = true;
  else rec.isExternal = false;
  if (raw.deleted === true) rec.deleted = true;
  return rec;
}

export function toChannelRecord(raw: Obj): ChannelRecord {
  const rec: ChannelRecord = { id: String(raw.id) };
  const name = s(raw.name);
  if (name) rec.name = name;
  if (typeof raw.is_im === "boolean") rec.isIm = raw.is_im;
  if (typeof raw.is_mpim === "boolean") rec.isMpim = raw.is_mpim;
  if (typeof raw.is_private === "boolean") rec.isPrivate = raw.is_private;
  else if (raw.is_group === true || raw.is_im === true || raw.is_mpim === true) rec.isPrivate = true;
  else if (raw.is_channel === true) rec.isPrivate = false;
  if (s(raw.user)) rec.imUserId = s(raw.user)!;
  return rec;
}

export function toDndInfo(raw: Obj): DndInfo {
  return {
    enabled: raw.dnd_enabled === true,
    nextStartTs: typeof raw.next_dnd_start_ts === "number" ? raw.next_dnd_start_ts : 0,
    nextEndTs: typeof raw.next_dnd_end_ts === "number" ? raw.next_dnd_end_ts : 0,
  };
}

export interface HydrationResult {
  complete: boolean;
  /** Completes all lookups and re-renders the draft. */
  remaining(): Promise<void>;
}

/** Entity resolution cache shared by event hydration and the users/channels APIs. */
export class Hydrator {
  readonly users: BatchLoader<UserRecord>;
  readonly channels: BatchLoader<ChannelRecord>;
  private readonly ims: BatchLoader<ChannelRecord>;
  private readonly usergroups = new Map<string, UsergroupRecord>();
  readonly mySubteams = new Set<string>();
  /** message ts → thread root ts (null = not in a thread) */
  private readonly threadRoots = new LruCache<string, string | null>(20_000, 60 * 60_000);

  constructor(
    private readonly core: GabrielCore,
    private readonly logger: Logger,
  ) {
    const teamId = () => core.session.teamId;
    this.users = new BatchLoader<UserRecord>({
      batchFn: async (ids) => {
        const res = await core.callEdgeApi<{ results?: Obj[] }>("users/info", {
          check_interaction: true,
          include_profile_only_users: true,
          updated_ids: Object.fromEntries(ids.map((id) => [id, 0])),
        });
        const out = new Map<string, UserRecord>();
        for (const raw of res.results ?? []) if (s(raw.id)) out.set(String(raw.id), toUserRecord(raw, teamId()));
        return out;
      },
    });
    this.channels = new BatchLoader<ChannelRecord>({
      batchFn: async (ids) => {
        const res = await core.callEdgeApi<{ results?: Obj[] }>("channels/info", {
          check_membership: true,
          updated_ids: Object.fromEntries(ids.map((id) => [id, 0])),
        });
        const out = new Map<string, ChannelRecord>();
        for (const raw of res.results ?? []) if (s(raw.id)) out.set(String(raw.id), toChannelRecord(raw));
        return out;
      },
    });
    // 1:1 DMs are not served by EdgeAPI; resolve them one by one via conversations.info.
    this.ims = new BatchLoader<ChannelRecord>({
      maxBatch: 10,
      concurrency: 1,
      batchFn: async (ids) => {
        const out = new Map<string, ChannelRecord>();
        for (const id of ids) {
          try {
            const res = await core.callWebApi<{ channel?: Obj }>("conversations.info", { channel: id }, { reason: "resolve-dm-info" });
            if (res.channel) out.set(id, toChannelRecord(res.channel));
          } catch (err) {
            if ((err as { code?: string }).code !== "CHANNEL_NOT_FOUND") throw err;
          }
        }
        return out;
      },
    });
  }

  /** Synchronous name lookup used for Markdown rendering. */
  readonly resolver: EntityResolver = {
    userName: (id) => this.users.peek(id)?.displayName,
    channelName: (id) => {
      const ch = this.peekChannel(id);
      if (!ch) return undefined;
      if (ch.isIm && ch.imUserId) {
        const peer = this.users.peek(ch.imUserId)?.displayName;
        return peer ? `@${peer}` : undefined;
      }
      return ch.name;
    },
    usergroupHandle: (id) => this.usergroups.get(id)?.handle || undefined,
  };

  peekChannel(id: string): ChannelRecord | undefined {
    return (id.startsWith("D") ? this.ims.peek(id) : this.channels.peek(id)) ?? undefined;
  }

  loadChannel(id: string, opts?: { fresh?: boolean }): Promise<ChannelRecord | null> {
    return id.startsWith("D") ? this.ims.load(id, opts) : this.channels.load(id, opts);
  }

  primeChannel(rec: ChannelRecord): void {
    (rec.id.startsWith("D") ? this.ims : this.channels).prime(rec.id, rec);
  }

  invalidateChannel(id: string): void {
    (id.startsWith("D") ? this.ims : this.channels).invalidate(id);
  }

  usergroup(id: string): UsergroupRecord | undefined {
    return this.usergroups.get(id);
  }

  upsertUsergroup(rec: UsergroupRecord): void {
    const prev = this.usergroups.get(rec.id);
    this.usergroups.set(rec.id, { ...prev, ...rec });
  }

  recordThreadRoot(ts: string, rootTs: string | null): void {
    this.threadRoots.set(ts, rootTs);
  }

  /** Loads usergroup handles and my memberships (best effort). */
  async preloadUsergroups(selfUserId: string): Promise<void> {
    try {
      const res = await this.core.callWebApi<{ usergroups?: Obj[] }>("usergroups.list", { include_users: true }, { reason: "gabriel-usergroups" });
      for (const g of res.usergroups ?? []) {
        const id = s(g.id);
        if (!id) continue;
        const users = Array.isArray(g.users) ? (g.users as unknown[]).filter((u): u is string => typeof u === "string") : undefined;
        this.upsertUsergroup({ id, handle: s(g.handle) ?? "", ...(s(g.name) ? { name: s(g.name)! } : {}), ...(users ? { users } : {}) });
        if (users?.includes(selfUserId)) this.mySubteams.add(id);
      }
    } catch (err) {
      this.logger.warn({ err: String(err) }, "usergroup preload failed; usergroup mentions may be incomplete");
    }
  }

  /** Thread root of a message ts within a channel (null = not threaded). */
  async threadRootOf(channel: string, ts: string): Promise<string | null> {
    const cached = this.threadRoots.get(ts);
    if (cached !== undefined) return cached;
    const res = await this.core.callWebApi<{ messages?: Obj[] }>(
      "conversations.replies",
      { channel, ts, limit: 1, inclusive: true },
      { reason: "history-api/fetchReplies" },
    );
    const first = res.messages?.[0];
    const root = first ? s(first.thread_ts) ?? null : null;
    this.threadRoots.set(ts, root);
    return root;
  }

  /** Starts entity lookups for a draft and renders it within `budgetMs`. */
  hydrate(draft: Draft, budgetMs: number): Promise<HydrationResult> {
    const tasks: Array<Promise<unknown>> = [];
    for (const id of draft.needs.users) if (this.users.peek(id) === undefined) tasks.push(this.users.load(id));
    for (const id of draft.needs.channels) {
      if (this.peekChannel(id) === undefined) {
        tasks.push(
          this.loadChannel(id).then((ch) => {
            if (ch?.imUserId && this.users.peek(ch.imUserId) === undefined) return this.users.load(ch.imUserId);
            return undefined;
          }),
        );
      }
    }
    const target = draft.reactionTarget;
    if (target) {
      tasks.push(
        this.threadRootOf(target.channel, target.ts).then((root) => {
          if (root) (draft.event as ReactionEvent).target.threadRootTs = root;
        }),
      );
    }
    const all = Promise.allSettled(tasks).then((results) => {
      for (const r of results) if (r.status === "rejected") this.logger.debug({ err: String(r.reason) }, "hydration lookup failed");
    });
    const finish = (complete: boolean): HydrationResult => {
      try {
        draft.render(this.resolver);
      } catch (err) {
        this.logger.error({ err: String(err) }, "render failed");
      }
      return {
        complete,
        remaining: async () => {
          await all;
          draft.render(this.resolver);
        },
      };
    };
    if (tasks.length === 0) return Promise.resolve(finish(true));
    let timer: NodeJS.Timeout | undefined;
    const budget = new Promise<false>((resolve) => {
      timer = setTimeout(() => resolve(false), budgetMs);
    });
    return Promise.race([all.then(() => true as const), budget]).then((complete) => {
      clearTimeout(timer);
      return finish(complete);
    });
  }

  /** DND lookup in batches of 30 (`dnd.teamInfo`). */
  async fetchDnd(ids: string[]): Promise<Record<string, DndInfo>> {
    const out: Record<string, DndInfo> = {};
    for (const batch of chunk([...new Set(ids)], 30)) {
      const res = await this.core.callWebApi<{ users?: Record<string, Obj> }>(
        "dnd.teamInfo",
        { users: batch.join(",") },
        { reason: "fetchAndUpsertDndForUsers" },
      );
      for (const [uid, raw] of Object.entries(res.users ?? {})) out[uid] = toDndInfo(raw);
    }
    return out;
  }
}
