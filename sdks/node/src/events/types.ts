/**
 * High-level event payloads. Every event is plain, JSON-serializable data that
 * mirrors the language-neutral schema in `spec/schema/events.schema.json`.
 * Convenience methods (e.g. `toLLMContext()`) are attached as non-enumerable
 * properties so `JSON.stringify(evt)` stays clean.
 */
export interface BaseEvent {
  type: string;
  /**
   * Globally unique event instance ID, usable for idempotency.
   * created: `${channel}:${ts}:created`; edits / deletes / reactions:
   * `${channel}:${targetTs}:${kind}:${eventTs}` (repeated edits never collide).
   */
  eventId: string;
}

export interface UserRef {
  id: string;
  name?: string;
  displayName?: string;
  handle?: string;
  isBot?: boolean;
  /** Member of another organization (Slack Connect). */
  isExternal?: boolean;
}

export interface ChannelRef {
  id: string;
  name?: string;
  isPrivate?: boolean;
  isIm?: boolean;
  isMpim?: boolean;
}

export type MessageOrigin = "this_client" | "self_other_client" | "other_user" | "bot";

export interface FileRef {
  id: string;
  name: string;
  title?: string;
  mimeType: string;
  size?: number;
  urlPrivate: string;
  permalink?: string;
}

export interface Mention {
  kind: "user" | "usergroup" | "broadcast" | "keyword";
  id: string;
  name?: string;
}

export interface ThreadInfo {
  rootTs: string;
  isReply: boolean;
  broadcast: boolean;
}

export interface MessageCreatedEvent extends BaseEvent {
  type: "message.created";
  ts: string;
  clientMsgId?: string;
  delivery: "realtime" | "backfill";
  origin: MessageOrigin;
  /** Slack mrkdwn as received. */
  text: string;
  /** Standard Markdown (mentions rendered as @name / #channel when resolved). */
  markdown: string;
  sender: UserRef;
  channel: ChannelRef;
  thread: ThreadInfo | null;
  isThreadReply: boolean;
  threadTs?: string;
  mentions: Mention[];
  /** Direct @me, a user group I belong to, or @here / @channel / @everyone. */
  isMentioningMe: boolean;
  files: FileRef[];
  /** Raw message subtype (`file_share`, `thread_broadcast`, `bot_message`, …) if any. */
  subtype?: string;
  raw: unknown;
  /** Untrusted-content-delimited LLM context (non-enumerable). */
  toLLMContext(): string;
}

export interface MessageUpdatedEvent extends BaseEvent {
  type: "message.updated";
  ts: string;
  channel: ChannelRef;
  sender: UserRef;
  origin: MessageOrigin;
  currentMarkdown: string;
  previousMarkdown?: string;
  editedAt: string;
  threadTs?: string;
  raw: unknown;
}

export interface MessageDeletedEvent extends BaseEvent {
  type: "message.deleted";
  deletedTs: string;
  channel: ChannelRef;
  previousMessage?: unknown;
  threadTs?: string;
}

export interface MessageThreadMetaUpdatedEvent extends BaseEvent {
  type: "message.thread_meta_updated";
  channel: ChannelRef;
  threadRootTs: string;
  replyCount: number;
  latestReplyTs: string;
}

export interface MessageOtherEvent extends BaseEvent {
  type: "message.other";
  rawSubtype: string;
  channel?: ChannelRef;
  ts?: string;
  rawFrame: unknown;
}

export interface AiStreamEvent extends BaseEvent {
  type: "ai.stream_updated" | "ai.stream_completed";
  ts: string;
  channel: ChannelRef;
  botProfile?: { id: string; name?: string; appId?: string };
  streamingState: "in_progress" | "completed";
  markdown: string;
  threadTs?: string;
}

export interface PresenceChangedEvent extends BaseEvent {
  type: "presence.changed";
  user: { id: string; displayName?: string; handle?: string };
  presence: "active" | "away";
  previousPresence: "active" | "away" | "unknown";
  /** true = initial snapshot when a subscription takes effect, not a real transition. */
  isInitialSnapshot: boolean;
}

export interface UserStatus {
  emoji: string;
  text: string;
  /** Unix seconds; 0 = never expires. */
  expiration: number;
}

export interface DndInfo {
  enabled: boolean;
  nextStartTs: number;
  nextEndTs: number;
}

export interface UserStatusChangedEvent extends BaseEvent {
  type: "user.status_changed";
  user: { id: string; displayName?: string; handle?: string };
  currentStatus: UserStatus;
  previousStatus?: UserStatus;
}

export interface UserDndChangedEvent extends BaseEvent {
  type: "user.dnd_changed";
  user: { id: string; displayName?: string; handle?: string };
  dnd: DndInfo;
  previousDnd?: DndInfo;
}

export interface UserSubteamUpdatedEvent extends BaseEvent {
  type: "user.subteam_updated";
  subteamId: string;
  handle: string;
  memberIds: string[];
}

export interface TypingEvent extends BaseEvent {
  type: "interaction.typing";
  user: { id: string; displayName?: string };
  channel: { id: string; name?: string };
  threadRootTs?: string;
}

export interface ReactionEvent extends BaseEvent {
  type: "interaction.reaction_added" | "interaction.reaction_removed";
  reaction: string;
  user: { id: string; displayName?: string };
  channel: { id: string; name?: string };
  target: {
    type: "message" | "file";
    ts?: string;
    threadRootTs?: string;
    fileId?: string;
  };
  itemUserId?: string;
}

export interface ChannelLifecycleEvent extends BaseEvent {
  type: "channel.lifecycle";
  action: "joined" | "left" | "im_created" | "renamed" | "archived";
  channel: ChannelRef;
}

export interface ChannelMarkedEvent extends BaseEvent {
  type: "channel.marked";
  channel: ChannelRef;
  lastReadTs: string;
  unreadCount: number;
  mentionCount: number;
}

export interface UnreadActivityEvent extends BaseEvent {
  type: "unread.activity";
  activityType: string;
  isUnread?: boolean;
  channel?: ChannelRef;
  threadTs?: string;
  itemTs?: string;
}

export type ActivityItemType =
  | "at_user" | "at_user_group" | "at_channel" | "at_everyone" | "keyword"
  | "channel" | "dm" | "thread_v2" | "message_reaction" | "bot_dm_bundle"
  | "internal_channel_invite" | "external_channel_invite" | "external_dm_invite"
  | "shared_workspace_invite" | "team_joiner_invite" | "unjoined_channel_mention"
  | "quietly_added_to_channel" | "prejoin_dm_welcome_party_alert" | "saved_reminder"
  | "list_record_assigned" | "list_record_edited" | "list_user_mentioned"
  | "list_todo_notification" | "list_approval_request" | "list_approval_reviewed"
  | "code_review_requested";

export interface UnreadBadgeCountsEvent extends BaseEvent {
  type: "unread.badge_counts";
  mode: "activity_v2" | "legacy";
  activity?: Partial<Record<ActivityItemType, number>> & Record<string, number>;
  mentionCount?: number;
  counts?: { dms: number; appDms: number; channels: number; threadMentions: number; threadUnreads: number };
  alerts?: Record<string, number>;
  raw: unknown;
}

export interface LoopDetectedEvent extends BaseEvent {
  type: "guard.loop_detected";
  channelId: string;
  threadTs?: string;
  replyCountInWindow: number;
}

export interface ProtocolDriftEvent extends BaseEvent {
  type: "guard.protocol_drift";
  unknownType: string;
  unknownSubtype?: string;
  sampleFrame: unknown;
}

export interface OverflowEvent extends BaseEvent {
  type: "guard.overflow";
  channelId?: string;
  ts?: string;
}

export interface LeaseExpiredEvent extends BaseEvent {
  type: "guard.lease_expired";
  targetEventId: string;
  handlerId: string;
  ownerReplicaId: string;
  delivery: "at_most_once" | "at_least_once";
  action: "alert_only" | "taken_over";
}

export interface ConnectionReadyEvent extends BaseEvent {
  type: "connection.ready";
  fastReconnect: boolean;
  degraded: boolean;
}

export interface ConnectionReconnectingEvent extends BaseEvent {
  type: "connection.reconnecting";
  attempt: number;
  delayMs: number;
}

export interface ConnectionGapEvent extends BaseEvent {
  type: "connection.gap";
  cause: "fast_reconnect_reconcile" | "cold_resync" | "overflow" | "state_store_unavailable";
  recovered: boolean;
  fromTs?: string;
  toTs?: string;
  recoveredCount: number;
  channelsRecovered: string[];
  threadsRecovered: Array<{ channel: string; threadTs: string }>;
  truncated: boolean;
  unrecovered: Array<{
    channel: string;
    threadTs?: string;
    fromTs: string;
    toTs?: string;
    reason: "page_limit" | "api_error" | "rate_limited" | "buffer_overflow";
  }>;
}

export interface AuthRefreshedEvent extends BaseEvent {
  type: "auth.refreshed";
  teamId: string;
  trigger: "invalid_auth" | "token_expired";
  refreshedAt: number;
}

export interface AuthDeadEvent extends BaseEvent {
  type: "auth.dead";
  teamId: string;
  reason: string;
}

export interface GabrielEventMap {
  "message.created": MessageCreatedEvent;
  "message.updated": MessageUpdatedEvent;
  "message.deleted": MessageDeletedEvent;
  "message.thread_meta_updated": MessageThreadMetaUpdatedEvent;
  "message.other": MessageOtherEvent;
  "ai.stream_updated": AiStreamEvent;
  "ai.stream_completed": AiStreamEvent;
  "presence.changed": PresenceChangedEvent;
  "user.status_changed": UserStatusChangedEvent;
  "user.dnd_changed": UserDndChangedEvent;
  "user.subteam_updated": UserSubteamUpdatedEvent;
  "interaction.typing": TypingEvent;
  "interaction.reaction_added": ReactionEvent;
  "interaction.reaction_removed": ReactionEvent;
  "channel.lifecycle": ChannelLifecycleEvent;
  "channel.marked": ChannelMarkedEvent;
  "unread.activity": UnreadActivityEvent;
  "unread.badge_counts": UnreadBadgeCountsEvent;
  "connection.ready": ConnectionReadyEvent;
  "connection.reconnecting": ConnectionReconnectingEvent;
  "connection.gap": ConnectionGapEvent;
  "auth.refreshed": AuthRefreshedEvent;
  "auth.dead": AuthDeadEvent;
  "guard.loop_detected": LoopDetectedEvent;
  "guard.protocol_drift": ProtocolDriftEvent;
  "guard.overflow": OverflowEvent;
  "guard.lease_expired": LeaseExpiredEvent;
}

export type GabrielEventType = keyof GabrielEventMap;
export type GabrielEvent = GabrielEventMap[GabrielEventType];

/** Message-created events are the only ones that advance recovery watermarks. */
export const MESSAGE_EVENT_TYPES: ReadonlySet<string> = new Set([
  "message.created",
  "message.updated",
  "message.deleted",
  "message.thread_meta_updated",
  "message.other",
  "ai.stream_updated",
  "ai.stream_completed",
]);
