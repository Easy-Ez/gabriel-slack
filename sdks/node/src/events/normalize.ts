import type { RawWsFrame } from "../core/types.js";
import { convertBlocksToMarkdown, mrkdwnToMarkdown, type EntityResolver } from "../markdown/blocks-to-markdown.js";
import { createHash } from "node:crypto";
import type {
  AiStreamEvent,
  ChannelLifecycleEvent,
  ChannelMarkedEvent,
  FileRef,
  GabrielEvent,
  Mention,
  MessageCreatedEvent,
  MessageDeletedEvent,
  MessageOrigin,
  MessageOtherEvent,
  MessageThreadMetaUpdatedEvent,
  MessageUpdatedEvent,
  ReactionEvent,
  TypingEvent,
  UnreadActivityEvent,
  UnreadBadgeCountsEvent,
  UserSubteamUpdatedEvent,
} from "./types.js";

/** Callbacks the normalizer needs from the stateful client. All optional for pure use. */
export interface NormalizeContext {
  selfUserId: string;
  /** Classifies a message author; defaults to the stateless rules (no self-send registry). */
  classifyOrigin?(info: { user?: string; botId?: string; clientMsgId?: string; ts: string; contentHash: string; isEdit: boolean }): MessageOrigin;
  /** Whether the current user belongs to the given user group. */
  isMySubteam?(subteamId: string): boolean;
  /** Recorded at receive time; defaults to "realtime". */
  delivery?: "realtime" | "backfill";
}

export interface EntityNeeds {
  users: Set<string>;
  channels: Set<string>;
  usergroups: Set<string>;
}

/** Internal routing envelope around one high-level event. */
export interface Draft {
  event: GabrielEvent;
  laneKey: string;
  channelId?: string;
  /** Thread root (or own ts) for thread-level executor keys. */
  threadKey?: string;
  needs: EntityNeeds;
  /** Re-renders names / markdown with the current resolver. */
  render(r: EntityResolver): void;
  /** Whether the event participates in eventId de-duplication. */
  dedupe: boolean;
  /** Set for message.created only — the ts that advances recovery watermarks. */
  createdTs?: string;
  /** Reaction drafts whose target thread root must be looked up. */
  reactionTarget?: { channel: string; ts: string };
}

const MESSAGE_CREATED_SUBTYPES = new Set(["file_share", "thread_broadcast", "bot_message", "me_message"]);
const str = (v: unknown): string | undefined => (typeof v === "string" && v.length > 0 ? v : undefined);
const obj = (v: unknown): Record<string, unknown> => (v && typeof v === "object" ? (v as Record<string, unknown>) : {});

function emptyNeeds(): EntityNeeds {
  return { users: new Set(), channels: new Set(), usergroups: new Set() };
}

/** Deterministic lane key — depends only on raw frame fields, never on hydration results. */
export function laneKeyOf(frame: RawWsFrame): string {
  const channel = frame.channel;
  if (typeof channel === "string") return channel;
  if (channel && typeof channel === "object" && typeof (channel as { id?: unknown }).id === "string") return (channel as { id: string }).id;
  const item = obj(frame.item);
  if (typeof item.channel === "string") return item.channel;
  const subteam = obj(frame.subteam);
  if (typeof subteam.id === "string") return subteam.id;
  if (typeof frame.user === "string") return frame.user;
  return "__global__";
}

export function contentHash(message: Record<string, unknown>): string {
  const files = Array.isArray(message.files) ? (message.files as Array<Record<string, unknown>>).map((f) => f.id) : [];
  const payload = JSON.stringify([message.text ?? "", message.blocks ?? null, files, message.attachments ?? null]);
  return createHash("sha256").update(payload).digest("hex").slice(0, 32);
}

export function defaultOrigin(selfUserId: string, user: string | undefined, botId: string | undefined): MessageOrigin {
  if (user !== undefined && user === selfUserId && botId === undefined) return "self_other_client";
  if (botId !== undefined) return "bot";
  return "other_user";
}

export function toFileRefs(files: unknown): FileRef[] {
  if (!Array.isArray(files)) return [];
  const out: FileRef[] = [];
  for (const raw of files) {
    const f = obj(raw);
    const id = str(f.id);
    if (!id) continue;
    const ref: FileRef = {
      id,
      name: str(f.name) ?? id,
      mimeType: str(f.mimetype) ?? "application/octet-stream",
      urlPrivate: str(f.url_private) ?? str(f.url_private_download) ?? "",
    };
    const title = str(f.title);
    if (title) ref.title = title;
    if (typeof f.size === "number") ref.size = f.size;
    const permalink = str(f.permalink);
    if (permalink) ref.permalink = permalink;
    out.push(ref);
  }
  return out;
}

function walkElements(node: unknown, visit: (el: Record<string, unknown>) => void): void {
  if (Array.isArray(node)) {
    for (const n of node) walkElements(n, visit);
    return;
  }
  const o = obj(node);
  if (Object.keys(o).length === 0) return;
  visit(o);
  if (Array.isArray(o.elements)) walkElements(o.elements, visit);
}

const TEXT_MENTION = /<@([UW][A-Z0-9]+)(?:\|[^>]*)?>|<!subteam\^(S[A-Z0-9]+)(?:\|[^>]*)?>|<!(here|channel|everyone)(?:\|[^>]*)?>/g;

/** Extracts mentions from blocks (preferred) or mrkdwn text. */
export function extractMentions(blocks: unknown, text: string | undefined): Mention[] {
  const seen = new Set<string>();
  const out: Mention[] = [];
  const add = (m: Mention) => {
    const k = `${m.kind}:${m.id}`;
    if (!seen.has(k)) {
      seen.add(k);
      out.push(m);
    }
  };
  walkElements(blocks, (el) => {
    if (el.type === "user" && typeof el.user_id === "string") add({ kind: "user", id: el.user_id });
    else if (el.type === "usergroup" && typeof el.usergroup_id === "string") add({ kind: "usergroup", id: el.usergroup_id });
    else if (el.type === "broadcast") add({ kind: "broadcast", id: str(el.range) ?? "channel" });
  });
  if (out.length === 0 && text) {
    for (const m of text.matchAll(TEXT_MENTION)) {
      if (m[1]) add({ kind: "user", id: m[1] });
      else if (m[2]) add({ kind: "usergroup", id: m[2] });
      else if (m[3]) add({ kind: "broadcast", id: m[3] });
    }
  }
  return out;
}

/** Renders a message body to Markdown: blocks first, then mrkdwn text, then legacy attachments. */
export function renderMessageMarkdown(message: Record<string, unknown>, r: EntityResolver): string {
  let md = convertBlocksToMarkdown(message.blocks, r);
  if (!md) md = mrkdwnToMarkdown(typeof message.text === "string" ? message.text : "", r);
  if (Array.isArray(message.attachments)) {
    const parts: string[] = [];
    for (const raw of message.attachments) {
      const a = obj(raw);
      const blocksMd = convertBlocksToMarkdown(a.blocks, r);
      const body = blocksMd || [str(a.pretext), str(a.title), str(a.text)].filter(Boolean).map((t) => mrkdwnToMarkdown(t!, r)).join("\n") || str(a.fallback);
      if (body) parts.push(body);
    }
    if (parts.length) md = [md, ...parts].filter(Boolean).join("\n\n");
  }
  return md;
}

function threadOf(message: Record<string, unknown>, ts: string, subtype: string | undefined) {
  const threadTs = str(message.thread_ts);
  if (!threadTs) return null;
  return { rootTs: threadTs, isReply: threadTs !== ts, broadcast: subtype === "thread_broadcast" };
}

function originFor(ctx: NormalizeContext, message: Record<string, unknown>, ts: string, isEdit: boolean): MessageOrigin {
  const user = str(message.user);
  const botId = str(message.bot_id);
  const clientMsgId = str(message.client_msg_id);
  if (ctx.classifyOrigin) {
    return ctx.classifyOrigin({
      ...(user === undefined ? {} : { user }),
      ...(botId === undefined ? {} : { botId }),
      ...(clientMsgId === undefined ? {} : { clientMsgId }),
      ts,
      contentHash: contentHash(message),
      isEdit,
    });
  }
  return defaultOrigin(ctx.selfUserId, user, botId);
}

function senderOf(message: Record<string, unknown>) {
  const botProfile = obj(message.bot_profile);
  const id = str(message.user) ?? str(botProfile.user_id) ?? str(message.bot_id) ?? "unknown";
  const isBot = Boolean(message.bot_id) || message.subtype === "bot_message";
  const name = str(botProfile.name) ?? str(message.username);
  return { id, isBot, ...(name ? { name, displayName: name } : {}) };
}

function collectMessageNeeds(needs: EntityNeeds, message: Record<string, unknown>, mentions: Mention[]): void {
  const user = str(message.user);
  if (user && !message.bot_profile) needs.users.add(user);
  for (const m of mentions) {
    if (m.kind === "user") needs.users.add(m.id);
    if (m.kind === "usergroup") needs.usergroups.add(m.id);
  }
  walkElements(message.blocks, (el) => {
    if (el.type === "channel" && typeof el.channel_id === "string") needs.channels.add(el.channel_id);
  });
  if (typeof message.text === "string") {
    for (const m of message.text.matchAll(/<#([CG][A-Z0-9]+)(?:\|[^>]*)?>/g)) needs.channels.add(m[1]!);
  }
}

function makeChannelRef(id: string) {
  return { id };
}

function userRefFromResolver(id: string, r: EntityResolver) {
  const displayName = r.userName?.(id);
  return displayName ? { id, displayName } : { id };
}

function normalizeMessageCreated(frame: RawWsFrame, ctx: NormalizeContext): Draft | null {
  const channel = str(frame.channel);
  const ts = str(frame.ts);
  if (!channel || !ts) return null;
  const subtype = str(frame.subtype);
  const text = typeof frame.text === "string" ? frame.text : "";
  const mentions = extractMentions(frame.blocks, text);
  const thread = threadOf(frame, ts, subtype);
  const isMySubteam = ctx.isMySubteam ?? (() => false);
  const isMentioningMe = mentions.some(
    (m) => (m.kind === "user" && m.id === ctx.selfUserId) || m.kind === "broadcast" || (m.kind === "usergroup" && isMySubteam(m.id)),
  );
  const clientMsgId = str(frame.client_msg_id);
  const evt = {
    type: "message.created",
    eventId: `${channel}:${ts}:created`,
    ts,
    ...(clientMsgId ? { clientMsgId } : {}),
    delivery: ctx.delivery ?? "realtime",
    origin: originFor(ctx, frame, ts, false),
    text,
    markdown: "",
    sender: senderOf(frame),
    channel: makeChannelRef(channel),
    thread,
    isThreadReply: thread?.isReply === true,
    ...(thread ? { threadTs: thread.rootTs } : {}),
    mentions,
    isMentioningMe,
    files: toFileRefs(frame.files),
    ...(subtype ? { subtype } : {}),
    raw: frame,
  } as unknown as MessageCreatedEvent;
  const needs = emptyNeeds();
  needs.channels.add(channel);
  collectMessageNeeds(needs, frame, mentions);
  return {
    event: evt,
    laneKey: channel,
    channelId: channel,
    threadKey: thread?.rootTs ?? ts,
    needs,
    dedupe: true,
    createdTs: ts,
    render(r) {
      evt.markdown = renderMessageMarkdown(frame, r);
      if (!evt.sender.isBot) {
        const name = r.userName?.(evt.sender.id);
        if (name) evt.sender.displayName = name;
      }
      for (const m of evt.mentions) {
        const name = m.kind === "user" ? r.userName?.(m.id) : m.kind === "usergroup" ? r.usergroupHandle?.(m.id) : undefined;
        if (name) m.name = name;
      }
      const cname = r.channelName?.(channel);
      if (cname) evt.channel.name = cname;
    },
  };
}

function normalizeMessageChanged(frame: RawWsFrame, ctx: NormalizeContext): Draft | null {
  const channel = str(frame.channel);
  const message = obj(frame.message);
  const previous = frame.previous_message === undefined ? undefined : obj(frame.previous_message);
  const ts = str(message.ts) ?? str(frame.ts);
  if (!channel || !ts) return null;
  const eventTs = str(frame.event_ts) ?? str(frame.ts) ?? ts;
  const threadTs = str(message.thread_ts);
  const aiContext = obj(message.ai_context);
  const streaming = str(message.streaming_state);
  const prevStreaming = previous ? str(previous.streaming_state) : undefined;
  const isAiStreaming =
    streaming !== undefined || prevStreaming === "in_progress" || aiContext.result_status === "streaming" || aiContext.result_status === "complete";
  const needs = emptyNeeds();
  needs.channels.add(channel);

  if (isAiStreaming) {
    const inProgress = streaming === "in_progress" || aiContext.result_status === "streaming";
    const type = inProgress ? "ai.stream_updated" : "ai.stream_completed";
    const bot = obj(message.bot_profile);
    const botId = str(bot.id) ?? str(message.bot_id);
    const evt: AiStreamEvent = {
      type,
      eventId: `${channel}:${ts}:${inProgress ? "ai_stream_updated" : "ai_stream_completed"}:${eventTs}`,
      ts,
      channel: makeChannelRef(channel),
      ...(botId ? { botProfile: { id: botId, ...(str(bot.name) ? { name: str(bot.name)! } : {}), ...(str(bot.app_id) ? { appId: str(bot.app_id)! } : {}) } } : {}),
      streamingState: inProgress ? "in_progress" : "completed",
      markdown: "",
      ...(threadTs ? { threadTs } : {}),
    };
    return {
      event: evt,
      laneKey: channel,
      channelId: channel,
      threadKey: threadTs ?? ts,
      needs,
      dedupe: true,
      render(r) {
        evt.markdown = renderMessageMarkdown(message, r);
        const cname = r.channelName?.(channel);
        if (cname) evt.channel.name = cname;
      },
    };
  }

  // Metadata-only change (reply_count, unfurl bookkeeping…): body hash unchanged → no event.
  if (previous && contentHash(previous) === contentHash(message)) return null;

  const mentions = extractMentions(message.blocks, typeof message.text === "string" ? message.text : undefined);
  collectMessageNeeds(needs, message, mentions);
  const edited = obj(message.edited);
  const evt: MessageUpdatedEvent = {
    type: "message.updated",
    eventId: `${channel}:${ts}:updated:${eventTs}`,
    ts,
    channel: makeChannelRef(channel),
    sender: senderOf(message),
    origin: originFor(ctx, message, ts, true),
    currentMarkdown: "",
    editedAt: str(edited.ts) ?? eventTs,
    ...(threadTs ? { threadTs } : {}),
    raw: frame,
  };
  if (previous) collectMessageNeeds(needs, previous, extractMentions(previous.blocks, str(previous.text)));
  return {
    event: evt,
    laneKey: channel,
    channelId: channel,
    threadKey: threadTs ?? ts,
    needs,
    dedupe: true,
    render(r) {
      evt.currentMarkdown = renderMessageMarkdown(message, r);
      if (previous) evt.previousMarkdown = renderMessageMarkdown(previous, r);
      const name = evt.sender.isBot ? undefined : r.userName?.(evt.sender.id);
      if (name) evt.sender.displayName = name;
      const cname = r.channelName?.(channel);
      if (cname) evt.channel.name = cname;
    },
  };
}

function normalizeMessageDeleted(frame: RawWsFrame): Draft | null {
  const channel = str(frame.channel);
  const deletedTs = str(frame.deleted_ts) ?? str(frame.ts);
  if (!channel || !deletedTs) return null;
  const eventTs = str(frame.event_ts) ?? str(frame.ts) ?? deletedTs;
  const previous = frame.previous_message;
  const threadTs = str(obj(previous).thread_ts);
  const evt: MessageDeletedEvent = {
    type: "message.deleted",
    eventId: `${channel}:${deletedTs}:deleted:${eventTs}`,
    deletedTs,
    channel: makeChannelRef(channel),
    ...(previous !== undefined ? { previousMessage: previous } : {}),
    ...(threadTs ? { threadTs } : {}),
  };
  const needs = emptyNeeds();
  needs.channels.add(channel);
  return {
    event: evt,
    laneKey: channel,
    channelId: channel,
    threadKey: threadTs ?? deletedTs,
    needs,
    dedupe: true,
    render(r) {
      const cname = r.channelName?.(channel);
      if (cname) evt.channel.name = cname;
    },
  };
}

function normalizeMessageReplied(frame: RawWsFrame): Draft | null {
  const channel = str(frame.channel);
  const parent = obj(frame.message);
  const root = str(parent.thread_ts) ?? str(parent.ts) ?? str(frame.ts);
  if (!channel || !root) return null;
  const latest = str(parent.latest_reply) ?? "";
  const evt: MessageThreadMetaUpdatedEvent = {
    type: "message.thread_meta_updated",
    eventId: `${channel}:${root}:thread_meta_updated:${str(frame.event_ts) ?? latest}`,
    channel: makeChannelRef(channel),
    threadRootTs: root,
    replyCount: typeof parent.reply_count === "number" ? parent.reply_count : 0,
    latestReplyTs: latest,
  };
  const needs = emptyNeeds();
  needs.channels.add(channel);
  return {
    event: evt,
    laneKey: channel,
    channelId: channel,
    threadKey: root,
    needs,
    dedupe: true,
    render(r) {
      const cname = r.channelName?.(channel);
      if (cname) evt.channel.name = cname;
    },
  };
}

function normalizeMessageOther(frame: RawWsFrame, subtype: string): Draft {
  const channel = str(frame.channel);
  const ts = str(frame.ts);
  const evt: MessageOtherEvent = {
    type: "message.other",
    eventId: `${channel ?? "-"}:${ts ?? "-"}:other_${subtype}:${str(frame.event_ts) ?? ts ?? "-"}`,
    rawSubtype: subtype,
    ...(channel ? { channel: makeChannelRef(channel) } : {}),
    ...(ts ? { ts } : {}),
    rawFrame: frame,
  };
  const needs = emptyNeeds();
  if (channel) needs.channels.add(channel);
  return {
    event: evt,
    laneKey: laneKeyOf(frame),
    ...(channel ? { channelId: channel } : {}),
    ...(ts ? { threadKey: str(frame.thread_ts) ?? ts } : {}),
    needs,
    dedupe: ts !== undefined,
    render(r) {
      if (channel && evt.channel) {
        const cname = r.channelName?.(channel);
        if (cname) evt.channel.name = cname;
      }
    },
  };
}

function normalizeTyping(frame: RawWsFrame): Draft | null {
  const channel = str(frame.channel);
  const user = str(frame.user);
  if (!channel || !user) return null;
  const threadTs = str(frame.thread_ts);
  const evt: TypingEvent = {
    type: "interaction.typing",
    eventId: `${channel}:typing:${user}:${typeof frame.id === "number" ? frame.id : "-"}`,
    user: { id: user },
    channel: { id: channel },
    ...(threadTs ? { threadRootTs: threadTs } : {}),
  };
  const needs = emptyNeeds();
  needs.users.add(user);
  needs.channels.add(channel);
  return {
    event: evt,
    laneKey: channel,
    channelId: channel,
    ...(threadTs ? { threadKey: threadTs } : {}),
    needs,
    dedupe: false,
    render(r) {
      Object.assign(evt.user, userRefFromResolver(user, r));
      const cname = r.channelName?.(channel);
      if (cname) evt.channel.name = cname;
    },
  };
}

function normalizeReaction(frame: RawWsFrame, added: boolean): Draft | null {
  const user = str(frame.user);
  const reaction = str(frame.reaction);
  const item = obj(frame.item);
  if (!user || !reaction) return null;
  const itemType = str(item.type);
  const channel = str(item.channel) ?? "";
  const ts = str(item.ts);
  const fileId = str(item.file) ?? str(item.file_comment);
  const kind = added ? "reaction_added" : "reaction_removed";
  const eventTs = str(frame.event_ts) ?? str(frame.ts) ?? "-";
  const itemUser = str(frame.item_user);
  const evt: ReactionEvent = {
    type: added ? "interaction.reaction_added" : "interaction.reaction_removed",
    eventId: `${channel || fileId || "-"}:${ts ?? fileId ?? "-"}:${kind}:${eventTs}:${user}:${reaction}`,
    reaction,
    user: { id: user },
    channel: { id: channel },
    target: {
      type: itemType === "message" ? "message" : "file",
      ...(ts ? { ts } : {}),
      ...(itemType !== "message" && fileId ? { fileId } : {}),
    },
    ...(itemUser ? { itemUserId: itemUser } : {}),
  };
  const needs = emptyNeeds();
  needs.users.add(user);
  if (channel) needs.channels.add(channel);
  return {
    event: evt,
    laneKey: channel || laneKeyOf(frame),
    ...(channel ? { channelId: channel } : {}),
    ...(ts ? { threadKey: ts } : {}),
    needs,
    dedupe: true,
    ...(channel && ts && itemType === "message" ? { reactionTarget: { channel, ts } } : {}),
    render(r) {
      Object.assign(evt.user, userRefFromResolver(user, r));
      const cname = channel ? r.channelName?.(channel) : undefined;
      if (cname) evt.channel.name = cname;
    },
  };
}

function normalizeMarked(frame: RawWsFrame): Draft | null {
  const channel = str(frame.channel);
  const ts = str(frame.ts);
  if (!channel || !ts) return null;
  const num = (...vals: unknown[]): number => {
    for (const v of vals) if (typeof v === "number") return v;
    return 0;
  };
  const evt: ChannelMarkedEvent = {
    type: "channel.marked",
    eventId: `${channel}:${ts}:marked:${str(frame.event_ts) ?? ts}`,
    channel: makeChannelRef(channel),
    lastReadTs: ts,
    unreadCount: num(frame.unread_count_display, frame.unread_count),
    mentionCount: num(frame.mention_count_display, frame.mention_count, frame.num_mentions_display, frame.num_mentions),
  };
  const needs = emptyNeeds();
  needs.channels.add(channel);
  return {
    event: evt,
    laneKey: channel,
    channelId: channel,
    needs,
    dedupe: false,
    render(r) {
      const cname = r.channelName?.(channel);
      if (cname) evt.channel.name = cname;
    },
  };
}

function normalizeActivity(frame: RawWsFrame): Draft | null {
  const entry = obj(frame.entry);
  const item = obj(entry.item);
  const message = obj(item.message);
  const activityType = str(item.type);
  if (!activityType) return null;
  const channel = str(message.channel) ?? str(item.channel);
  const threadTs = str(message.thread_ts) ?? str(item.thread_ts);
  const itemTs = str(message.ts) ?? str(entry.feed_ts);
  const key = str(entry.key) ?? str(frame.key) ?? activityType;
  const evt: UnreadActivityEvent = {
    type: "unread.activity",
    eventId: `activity:${key}:${str(frame.event_ts) ?? "-"}`,
    activityType,
    ...(typeof entry.is_unread === "boolean" ? { isUnread: entry.is_unread } : {}),
    ...(channel ? { channel: makeChannelRef(channel) } : {}),
    ...(threadTs ? { threadTs } : {}),
    ...(itemTs ? { itemTs } : {}),
  };
  const needs = emptyNeeds();
  if (channel) needs.channels.add(channel);
  return {
    event: evt,
    laneKey: channel ?? "__global__",
    ...(channel ? { channelId: channel } : {}),
    needs,
    dedupe: false,
    render(r) {
      if (channel && evt.channel) {
        const cname = r.channelName?.(channel);
        if (cname) evt.channel.name = cname;
      }
    },
  };
}

/** Mention roll-up used by the official client (`sumMentionCounts`). */
export const MENTION_ACTIVITY_KEYS = [
  "at_user",
  "at_user_group",
  "at_channel",
  "at_everyone",
  "keyword",
  "unjoined_channel_mention",
  "list_record_assigned",
  "list_user_mentioned",
  "list_todo_notification",
  "list_approval_request",
  "list_approval_reviewed",
] as const;

export function normalizeBadgeCounts(frame: RawWsFrame): Draft | null {
  const eventTs = str(frame.event_ts) ?? "-";
  let evt: UnreadBadgeCountsEvent;
  if (frame.activity_v2 && typeof frame.activity_v2 === "object") {
    const activity: Record<string, number> = {};
    for (const [k, v] of Object.entries(obj(frame.activity_v2))) if (typeof v === "number") activity[k] = v;
    const mentionCount = MENTION_ACTIVITY_KEYS.reduce((sum, k) => sum + (activity[k] ?? 0), 0);
    evt = { type: "unread.badge_counts", eventId: `badge_counts:${eventTs}`, mode: "activity_v2", activity, mentionCount, raw: frame };
  } else {
    const c = obj(frame.counts);
    const n = (v: unknown) => (typeof v === "number" ? v : 0);
    const alerts: Record<string, number> = {};
    for (const [k, v] of Object.entries(obj(frame.alerts))) if (typeof v === "number") alerts[k] = v;
    evt = {
      type: "unread.badge_counts",
      eventId: `badge_counts:${eventTs}`,
      mode: "legacy",
      counts: { dms: n(c.dms), appDms: n(c.app_dms), channels: n(c.channels), threadMentions: n(c.thread_mentions), threadUnreads: n(c.thread_unreads) },
      ...(Object.keys(alerts).length ? { alerts } : {}),
      raw: frame,
    };
  }
  return { event: evt, laneKey: "__global__", needs: emptyNeeds(), dedupe: false, render() {} };
}

/** Content key for badge snapshots: identical snapshots pushed back-to-back are dropped. */
export function badgeSnapshotKey(evt: UnreadBadgeCountsEvent): string {
  const sorted = (o: Record<string, number> | undefined) => (o ? Object.keys(o).sort().map((k) => `${k}=${o[k]}`).join(",") : "");
  return `${evt.mode}|${sorted(evt.activity)}|${evt.counts ? JSON.stringify(evt.counts) : ""}|${sorted(evt.alerts)}`;
}

const LIFECYCLE: Record<string, ChannelLifecycleEvent["action"]> = {
  channel_joined: "joined",
  group_joined: "joined",
  channel_left: "left",
  group_left: "left",
  im_created: "im_created",
  channel_rename: "renamed",
  group_rename: "renamed",
  channel_archive: "archived",
  group_archive: "archived",
};

function normalizeLifecycle(frame: RawWsFrame, ctx: NormalizeContext): Draft | null {
  const type = frame.type ?? "";
  let action = LIFECYCLE[type];
  if (type === "member_joined_channel" || type === "member_left_channel") {
    if (frame.user !== ctx.selfUserId) return null;
    action = type === "member_joined_channel" ? "joined" : "left";
  }
  if (!action) return null;
  const chObj = obj(frame.channel);
  const id = typeof frame.channel === "string" ? frame.channel : str(chObj.id);
  if (!id) return null;
  const channel: ChannelLifecycleEvent["channel"] = { id };
  const name = str(chObj.name);
  if (name) channel.name = name;
  if (typeof chObj.is_private === "boolean") channel.isPrivate = chObj.is_private;
  if (type === "im_created") channel.isIm = true;
  if (typeof chObj.is_mpim === "boolean") channel.isMpim = chObj.is_mpim;
  const evt: ChannelLifecycleEvent = {
    type: "channel.lifecycle",
    eventId: `${id}:lifecycle:${action}:${str(frame.event_ts) ?? "-"}`,
    action,
    channel,
  };
  const needs = emptyNeeds();
  if (!name && action !== "im_created") needs.channels.add(id);
  return {
    event: evt,
    laneKey: id,
    channelId: id,
    needs,
    dedupe: false,
    render(r) {
      if (!evt.channel.name) {
        const cname = r.channelName?.(id);
        if (cname) evt.channel.name = cname;
      }
    },
  };
}

function normalizeSubteamUpdated(frame: RawWsFrame): Draft | null {
  const subteam = obj(frame.subteam);
  const id = str(subteam.id);
  if (!id) return null;
  const evt: UserSubteamUpdatedEvent = {
    type: "user.subteam_updated",
    eventId: `subteam:${id}:${str(frame.event_ts) ?? String(subteam.date_update ?? "-")}`,
    subteamId: id,
    handle: str(subteam.handle) ?? "",
    memberIds: Array.isArray(subteam.users) ? (subteam.users as unknown[]).filter((u): u is string => typeof u === "string") : [],
  };
  return { event: evt, laneKey: id, needs: emptyNeeds(), dedupe: false, render() {} };
}

/**
 * Stateless mapping from one raw frame to at most one primary high-level event
 * ("one primary event per frame"). Stateful domains (presence, profile/DND
 * refetch, member-diff subteam updates) are produced by the client itself.
 */
export function normalizeFrame(frame: RawWsFrame, ctx: NormalizeContext): Draft | null {
  switch (frame.type) {
    case "message": {
      const subtype = str(frame.subtype);
      if (!subtype || MESSAGE_CREATED_SUBTYPES.has(subtype)) return normalizeMessageCreated(frame, ctx);
      if (subtype === "message_changed") return normalizeMessageChanged(frame, ctx);
      if (subtype === "message_deleted") return normalizeMessageDeleted(frame);
      if (subtype === "message_replied") return normalizeMessageReplied(frame);
      return normalizeMessageOther(frame, subtype);
    }
    case "user_typing":
      return normalizeTyping(frame);
    case "reaction_added":
      return normalizeReaction(frame, true);
    case "reaction_removed":
      return normalizeReaction(frame, false);
    case "channel_marked":
    case "im_marked":
    case "group_marked":
    case "mpim_marked":
      return normalizeMarked(frame);
    case "activity":
      return normalizeActivity(frame);
    case "badge_counts_updated":
      return normalizeBadgeCounts(frame);
    case "subteam_updated":
    case "subteam_created":
      return normalizeSubteamUpdated(frame);
    default:
      return normalizeLifecycle(frame, ctx);
  }
}
