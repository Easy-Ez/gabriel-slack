import { AsyncLocalStorage } from "node:async_hooks";
import { GabrielError } from "../errors.js";
import type { GabrielEvent, MessageCreatedEvent } from "../events/types.js";
import { compareTs } from "../util/validate.js";
import type { SendOptions, SendResult } from "./chat.js";
import type { StreamController } from "./stream.js";

export interface MessageMatcher {
  /** Channel ID allow-list. */
  channel?: string | string[];
  /** Equivalent to `evt.isMentioningMe === true`. */
  mentionsMe?: boolean;
  /** Tested against `evt.markdown`. */
  pattern?: RegExp;
}

export function matcherToPredicate(filter: MessageMatcher | ((evt: MessageCreatedEvent) => boolean)): (evt: MessageCreatedEvent) => boolean {
  if (typeof filter === "function") return filter;
  const channels = filter.channel === undefined ? undefined : new Set(Array.isArray(filter.channel) ? filter.channel : [filter.channel]);
  return (evt) => {
    if (channels && !channels.has(evt.channel.id)) return false;
    if (filter.mentionsMe === true && !evt.isMentioningMe) return false;
    if (filter.mentionsMe === false && evt.isMentioningMe) return false;
    if (filter.pattern) {
      filter.pattern.lastIndex = 0;
      if (!filter.pattern.test(evt.markdown)) return false;
    }
    return true;
  };
}

export interface ReplyOptions {
  markdown: string;
  /** Default true: reply in the trigger's thread (or start one under the trigger message). */
  inThread?: boolean;
  broadcast?: boolean;
  preserveUnreadCursor?: boolean;
}

export interface WaitForReplyOptions {
  timeoutMs: number;
  /** Default true: a matched reply goes only to this waiter, not to other handlers. */
  consume?: boolean;
  /** Default false: when true, this waiter receives a matched reply alone. */
  exclusive?: boolean;
}

/** Handler context (`ctx`). */
export interface EventContext {
  /**
   * Aborted when the triggering message is deleted, the handler exceeds
   * `handlerTimeoutMs`, or `client.stop()` drain time runs out.
   */
  readonly signal: AbortSignal;
  reply(markdownOrOpts: string | ReplyOptions): Promise<SendResult>;
  replyStream(opts?: { initialMarkdown?: string; preserveUnreadCursor?: boolean }): Promise<StreamController>;
  react(emoji: string): Promise<void>;
  waitForReply(filter: MessageMatcher | ((evt: MessageCreatedEvent) => boolean), opts: WaitForReplyOptions): Promise<MessageCreatedEvent | null>;
  hydrateRemaining(): Promise<void>;
}

/** Ambient trigger of the running handler (scope sandbox + loop breaker). */
export interface TriggerInfo {
  channel?: string;
  /** Thread the handler replies into: trigger's thread root, or the trigger ts itself. */
  threadRootTs?: string;
  ts?: string;
  eventId: string;
}

export const triggerStorage = new AsyncLocalStorage<TriggerInfo>();

export function currentTrigger(): TriggerInfo | undefined {
  return triggerStorage.getStore();
}

/** Derives where a reply to this event should go. */
export function triggerOf(evt: GabrielEvent): TriggerInfo {
  switch (evt.type) {
    case "message.created":
      return { channel: evt.channel.id, threadRootTs: evt.thread?.rootTs ?? evt.ts, ts: evt.ts, eventId: evt.eventId };
    case "message.updated":
      return { channel: evt.channel.id, threadRootTs: evt.threadTs ?? evt.ts, ts: evt.ts, eventId: evt.eventId };
    case "interaction.reaction_added":
    case "interaction.reaction_removed":
      return {
        channel: evt.channel.id || undefined,
        ...(evt.target.ts ? { threadRootTs: evt.target.threadRootTs ?? evt.target.ts, ts: evt.target.ts } : {}),
        eventId: evt.eventId,
      } as TriggerInfo;
    case "ai.stream_updated":
    case "ai.stream_completed":
      return { channel: evt.channel.id, threadRootTs: evt.threadTs ?? evt.ts, ts: evt.ts, eventId: evt.eventId };
    case "interaction.typing":
      return { channel: evt.channel.id, ...(evt.threadRootTs ? { threadRootTs: evt.threadRootTs } : {}), eventId: evt.eventId };
    default: {
      const ch = (evt as { channel?: { id?: string } }).channel?.id;
      return { ...(ch ? { channel: ch } : {}), eventId: evt.eventId };
    }
  }
}

interface Waiter {
  predicate: (evt: MessageCreatedEvent) => boolean;
  consume: boolean;
  exclusive: boolean;
  afterTs?: string;
  resolve: (evt: MessageCreatedEvent) => void;
}

const RECENT_WINDOW_MS = 10_000;

/**
 * Pre-executor reply router for `ctx.waitForReply` (spec/behavior.md §6.3). Matching
 * happens after hydration and before the executor queue, so replies reach
 * waiters even when the queue is full.
 */
export class WaitRegistry {
  private readonly waiters: Waiter[] = [];
  private recent: Array<{ evt: MessageCreatedEvent; at: number }> = [];

  /** Returns true when the event was consumed by a waiter. */
  route(evt: GabrielEvent): boolean {
    if (evt.type !== "message.created") return false;
    let consumed = false;
    let delivered = false;
    for (let i = 0; i < this.waiters.length; ) {
      const w = this.waiters[i]!;
      if ((w.afterTs && compareTs(evt.ts, w.afterTs) <= 0) || !safeTest(w.predicate, evt)) {
        i++;
        continue;
      }
      if (w.exclusive && delivered) {
        i++;
        continue;
      }
      this.waiters.splice(i, 1);
      w.resolve(evt);
      delivered = true;
      if (w.consume) consumed = true;
      if (w.exclusive) break;
    }
    if (!consumed) this.remember(evt);
    return consumed;
  }

  private remember(evt: MessageCreatedEvent): void {
    const now = Date.now();
    this.recent = this.recent.filter((r) => now - r.at < RECENT_WINDOW_MS);
    this.recent.push({ evt, at: now });
    if (this.recent.length > 500) this.recent.shift();
  }

  /** Scans recently routed, unconsumed messages (newer than `afterTs`). */
  takeRecent(predicate: (evt: MessageCreatedEvent) => boolean, afterTs: string | undefined, consume: boolean): MessageCreatedEvent | undefined {
    const now = Date.now();
    const idx = this.recent.findIndex(
      (r) => now - r.at < RECENT_WINDOW_MS && (!afterTs || compareTs(r.evt.ts, afterTs) > 0) && safeTest(predicate, r.evt),
    );
    if (idx < 0) return undefined;
    const [hit] = consume ? this.recent.splice(idx, 1) : [this.recent[idx]!];
    return hit!.evt;
  }

  add(waiter: Waiter): () => void {
    this.waiters.push(waiter);
    return () => {
      const i = this.waiters.indexOf(waiter);
      if (i >= 0) this.waiters.splice(i, 1);
    };
  }
}

function safeTest(predicate: (evt: MessageCreatedEvent) => boolean, evt: MessageCreatedEvent): boolean {
  try {
    return predicate(evt) === true;
  } catch {
    return false;
  }
}

export function assertWritable(cancelled: boolean): void {
  if (cancelled) throw new GabrielError("CANCELLED", "handler timed out or was cancelled; writes through this ctx are rejected");
}
