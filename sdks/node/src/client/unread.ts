import type { GabrielCore } from "../core/core.js";
import type { Logger } from "../util/logger.js";
import { compareTs, isTs } from "../util/validate.js";
import type { SelfRegistry } from "./origin.js";

interface ChannelGuard {
  inflight: number;
  baseline: Promise<{ lastRead: string; hasUnread: boolean } | undefined>;
  userRead: boolean;
  lastRead?: string;
}

/**
 * `preserveUnreadCursor` (spec/behavior.md §9): posting a message advances the channel's
 * `last_read`. The baseline is latched when the first concurrent post starts and
 * restored once after the last one finishes — unless the account owner reads the
 * channel meanwhile. Best effort: `conversations.mark` has no compare-and-swap.
 */
export class UnreadCursorGuard {
  private readonly channels = new Map<string, ChannelGuard>();

  constructor(
    private readonly core: GabrielCore,
    private readonly registry: SelfRegistry,
    private readonly logger: Logger,
  ) {}

  async run<T>(channel: string, enabled: boolean, fn: () => Promise<T>): Promise<T> {
    if (!enabled) return fn();
    let g = this.channels.get(channel);
    if (!g) {
      const created: ChannelGuard = { inflight: 0, userRead: false, baseline: this.fetchBaseline(channel) };
      void created.baseline.then((b) => {
        if (b) created.lastRead = b.lastRead;
      });
      this.channels.set(channel, (g = created));
    }
    g.inflight++;
    const guard = g;
    try {
      await guard.baseline; // latch before the first post advances the cursor
      return await fn();
    } finally {
      guard.inflight--;
      if (guard.inflight === 0) await this.restore(channel, guard);
    }
  }

  /** Feed of channel_marked-style frames. */
  onMarked(channel: string, ts: string): void {
    const g = this.channels.get(channel);
    if (!g || !g.lastRead) return;
    if (this.registry.isSelfMark(channel, ts)) return;
    if (compareTs(ts, g.lastRead) > 0) g.userRead = true;
  }

  private async fetchBaseline(channel: string): Promise<{ lastRead: string; hasUnread: boolean } | undefined> {
    try {
      const res = await this.core.callWebApi<{ channel?: Record<string, unknown> }>("conversations.info", { channel }, { reason: "gabriel-unread-baseline" });
      const ch = res.channel ?? {};
      const lastRead = typeof ch.last_read === "string" && isTs(ch.last_read) ? ch.last_read : undefined;
      const unread = [ch.unread_count, ch.unread_count_display].some((v) => typeof v === "number" && v > 0);
      return lastRead ? { lastRead, hasUnread: unread } : undefined;
    } catch (err) {
      this.logger.debug({ err: String(err), channel }, "unread baseline unavailable");
      return undefined;
    }
  }

  private async restore(channel: string, g: ChannelGuard): Promise<void> {
    try {
      const base = await g.baseline;
      if (!base || !base.hasUnread || g.userRead) return;
      this.registry.registerSelfMark(channel, base.lastRead);
      await this.core.callWebApi("conversations.mark", { channel, ts: base.lastRead }, { reason: "gabriel-preserve-unread" });
    } catch (err) {
      this.logger.warn({ err: String(err), channel }, "could not restore unread cursor");
    } finally {
      if (this.channels.get(channel) === g && g.inflight === 0) this.channels.delete(channel);
    }
  }
}
