"""High-level event payloads.

Every event is a dataclass with snake_case attributes that map 1:1 to the camelCase fields of
``spec/schema/events.schema.json``. :meth:`BaseEvent.to_dict` / :meth:`BaseEvent.to_json`
produce the schema form: optional fields that are ``None`` are omitted, nullable fields
(``MessageCreatedEvent.thread``) serialize as ``null``, and raw Slack payloads (``raw``,
``activity``, …) pass through unchanged.

Convenience methods (e.g. :meth:`MessageCreatedEvent.to_llm_context`) are not part of the
payload.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal, TypeVar

MessageOrigin = Literal["this_client", "self_other_client", "other_user", "bot"]
Presence = Literal["active", "away"]
#: How an agent attribution line ends (spec/behavior.md §9.1).
AttributionReview = Literal["confirmed", "unconfirmed", "hidden"]
#: Why a message mentions me: ``direct`` (I am mentioned by ID), ``usergroup`` (a group I belong to)
#: or ``broadcast`` (@here / @channel / @everyone). Keyword highlights never count.
MentionReason = Literal["direct", "usergroup", "broadcast"]

_NULLABLE = {"nullable": True}


def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _serialize(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _to_dict(value)
    if isinstance(value, list | tuple):
        return [_serialize(v) for v in value]
    return value


def _to_dict(obj: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    event_type = getattr(type(obj), "TYPE", None)
    if isinstance(event_type, str) and event_type:
        out["type"] = event_type
    for f in dataclasses.fields(obj):
        value = getattr(obj, f.name)
        if value is None and not f.metadata.get("nullable"):
            continue
        out[_camel(f.name)] = _serialize(value)
    return out


class _Serializable:
    __slots__ = ()

    def to_dict(self) -> dict[str, Any]:
        """Schema-shaped (camelCase) JSON-compatible dict."""
        return _to_dict(self)

    def to_json(self, **kwargs: Any) -> str:
        kwargs.setdefault("ensure_ascii", False)
        return json.dumps(self.to_dict(), **kwargs)


# --------------------------------------------------------------------------- shared shapes


@dataclass(kw_only=True, slots=True)
class UserRef(_Serializable):
    id: str
    name: str | None = None
    display_name: str | None = None
    handle: str | None = None
    is_bot: bool | None = None
    #: Member of another organization (Slack Connect).
    is_external: bool | None = None


@dataclass(kw_only=True, slots=True)
class ChannelRef(_Serializable):
    id: str
    name: str | None = None
    is_private: bool | None = None
    is_im: bool | None = None
    is_mpim: bool | None = None


@dataclass(kw_only=True, slots=True)
class FileRef(_Serializable):
    id: str
    name: str
    mime_type: str
    url_private: str
    title: str | None = None
    size: int | None = None
    permalink: str | None = None


@dataclass(kw_only=True, slots=True)
class Mention(_Serializable):
    kind: Literal["user", "usergroup", "broadcast", "keyword"]
    id: str
    name: str | None = None
    #: ``usergroup`` only: whether I belong to the group. ``None`` when the membership is unknown.
    includes_me: bool | None = None


@dataclass(kw_only=True, slots=True)
class MessageAttribution(_Serializable):
    """Self-declared agent attribution line found on a received message (spec §9.1). Unverified."""

    #: The line rendered to Markdown.
    text: str
    #: Present when the line was written by a preset.
    review: AttributionReview | None = None


@dataclass(kw_only=True, slots=True)
class ThreadInfo(_Serializable):
    root_ts: str
    is_reply: bool
    broadcast: bool


@dataclass(kw_only=True, slots=True)
class UserStatus(_Serializable):
    emoji: str
    text: str
    #: Unix seconds; 0 = never expires.
    expiration: int | float


@dataclass(kw_only=True, slots=True)
class DndInfo(_Serializable):
    enabled: bool
    next_start_ts: int | float
    next_end_ts: int | float


@dataclass(kw_only=True, slots=True)
class BotProfile(_Serializable):
    id: str
    name: str | None = None
    app_id: str | None = None


@dataclass(kw_only=True, slots=True)
class ReactionTarget(_Serializable):
    type: Literal["message", "file"]
    ts: str | None = None
    thread_root_ts: str | None = None
    file_id: str | None = None


@dataclass(kw_only=True, slots=True)
class BadgeCounts(_Serializable):
    dms: int
    app_dms: int
    channels: int
    thread_mentions: int
    thread_unreads: int


@dataclass(kw_only=True, slots=True)
class GapThread(_Serializable):
    channel: str
    thread_ts: str


@dataclass(kw_only=True, slots=True)
class GapRange(_Serializable):
    channel: str
    from_ts: str
    reason: Literal["page_limit", "api_error", "rate_limited", "buffer_overflow"]
    thread_ts: str | None = None
    to_ts: str | None = None


# --------------------------------------------------------------------------- events


@dataclass(kw_only=True, slots=True)
class BaseEvent(_Serializable):
    #: The event type string (``"message.created"``, …). Set per subclass.
    TYPE: ClassVar[str] = ""
    #: Globally unique event instance ID, usable for idempotency.
    #: created: ``{channel}:{ts}:created``; edits / deletes / reactions:
    #: ``{channel}:{target_ts}:{kind}:{event_ts}`` (repeated edits never collide).
    event_id: str

    @property
    def type(self) -> str:
        return type(self).TYPE


@dataclass(kw_only=True, slots=True)
class MessageCreatedEvent(BaseEvent):
    TYPE: ClassVar[str] = "message.created"
    ts: str
    delivery: Literal["realtime", "backfill"]
    origin: MessageOrigin
    #: Slack mrkdwn as received.
    text: str
    #: Standard Markdown (mentions rendered as @name / #channel when resolved).
    markdown: str
    sender: UserRef
    channel: ChannelRef
    thread: ThreadInfo | None = field(default=None, metadata=_NULLABLE)
    is_thread_reply: bool = False
    mentions: list[Mention] = field(default_factory=list)
    #: Why the message mentions me, each reason once, in the order direct, usergroup, broadcast;
    #: empty if it does not.
    mention_reasons: list[MentionReason] = field(default_factory=list)
    #: ``len(mention_reasons) > 0``: a direct @me, a user group I belong to, **or** @here / @channel /
    #: @everyone. To react only when I am named, use ``"direct" in mention_reasons``.
    is_mentioning_me: bool = False
    files: list[FileRef] = field(default_factory=list)
    raw: Any = None
    client_msg_id: str | None = None
    thread_ts: str | None = None
    #: Raw message subtype (``file_share``, ``thread_broadcast``, ``bot_message``, …) if any.
    subtype: str | None = None
    #: Self-declared agent attribution line, removed from ``markdown`` (spec §9.1). Unverified.
    attribution: MessageAttribution | None = None

    def to_llm_context(self) -> str:
        """Untrusted-content-delimited LLM context (see :func:`gabriel.to_llm_context`)."""
        from ..client.llm_context import to_llm_context

        return to_llm_context(self)


@dataclass(kw_only=True, slots=True)
class MessageUpdatedEvent(BaseEvent):
    TYPE: ClassVar[str] = "message.updated"
    ts: str
    channel: ChannelRef
    sender: UserRef
    origin: MessageOrigin
    current_markdown: str
    edited_at: str
    raw: Any = None
    previous_markdown: str | None = None
    thread_ts: str | None = None
    #: Attribution line of the current content (spec §9.1). Unverified.
    attribution: MessageAttribution | None = None


@dataclass(kw_only=True, slots=True)
class MessageDeletedEvent(BaseEvent):
    TYPE: ClassVar[str] = "message.deleted"
    deleted_ts: str
    channel: ChannelRef
    previous_message: Any = None
    thread_ts: str | None = None


@dataclass(kw_only=True, slots=True)
class MessageThreadMetaUpdatedEvent(BaseEvent):
    TYPE: ClassVar[str] = "message.thread_meta_updated"
    channel: ChannelRef
    thread_root_ts: str
    reply_count: int
    latest_reply_ts: str


@dataclass(kw_only=True, slots=True)
class MessageOtherEvent(BaseEvent):
    TYPE: ClassVar[str] = "message.other"
    raw_subtype: str
    raw_frame: Any = None
    channel: ChannelRef | None = None
    ts: str | None = None


@dataclass(kw_only=True, slots=True)
class AiStreamEvent(BaseEvent):
    """Base of :class:`AiStreamUpdatedEvent` and :class:`AiStreamCompletedEvent`."""

    ts: str
    channel: ChannelRef
    streaming_state: Literal["in_progress", "completed"]
    markdown: str
    bot_profile: BotProfile | None = None
    thread_ts: str | None = None


@dataclass(kw_only=True, slots=True)
class AiStreamUpdatedEvent(AiStreamEvent):
    TYPE: ClassVar[str] = "ai.stream_updated"


@dataclass(kw_only=True, slots=True)
class AiStreamCompletedEvent(AiStreamEvent):
    TYPE: ClassVar[str] = "ai.stream_completed"


@dataclass(kw_only=True, slots=True)
class PresenceChangedEvent(BaseEvent):
    TYPE: ClassVar[str] = "presence.changed"
    user: UserRef
    presence: Presence
    previous_presence: Literal["active", "away", "unknown"]
    #: True = initial snapshot when a subscription takes effect, not a real transition.
    is_initial_snapshot: bool


@dataclass(kw_only=True, slots=True)
class UserStatusChangedEvent(BaseEvent):
    TYPE: ClassVar[str] = "user.status_changed"
    user: UserRef
    current_status: UserStatus
    previous_status: UserStatus | None = None


@dataclass(kw_only=True, slots=True)
class UserDndChangedEvent(BaseEvent):
    TYPE: ClassVar[str] = "user.dnd_changed"
    user: UserRef
    dnd: DndInfo
    previous_dnd: DndInfo | None = None


@dataclass(kw_only=True, slots=True)
class UserSubteamUpdatedEvent(BaseEvent):
    TYPE: ClassVar[str] = "user.subteam_updated"
    subteam_id: str
    handle: str
    member_ids: list[str]


@dataclass(kw_only=True, slots=True)
class TypingEvent(BaseEvent):
    TYPE: ClassVar[str] = "interaction.typing"
    user: UserRef
    channel: ChannelRef
    thread_root_ts: str | None = None


@dataclass(kw_only=True, slots=True)
class ReactionEvent(BaseEvent):
    """Base of :class:`ReactionAddedEvent` and :class:`ReactionRemovedEvent`."""

    reaction: str
    user: UserRef
    channel: ChannelRef
    target: ReactionTarget
    item_user_id: str | None = None


@dataclass(kw_only=True, slots=True)
class ReactionAddedEvent(ReactionEvent):
    TYPE: ClassVar[str] = "interaction.reaction_added"


@dataclass(kw_only=True, slots=True)
class ReactionRemovedEvent(ReactionEvent):
    TYPE: ClassVar[str] = "interaction.reaction_removed"


@dataclass(kw_only=True, slots=True)
class ChannelLifecycleEvent(BaseEvent):
    TYPE: ClassVar[str] = "channel.lifecycle"
    action: Literal["joined", "left", "im_created", "renamed", "archived"]
    channel: ChannelRef


@dataclass(kw_only=True, slots=True)
class ChannelMarkedEvent(BaseEvent):
    TYPE: ClassVar[str] = "channel.marked"
    channel: ChannelRef
    last_read_ts: str
    unread_count: int
    mention_count: int


@dataclass(kw_only=True, slots=True)
class UnreadActivityEvent(BaseEvent):
    TYPE: ClassVar[str] = "unread.activity"
    activity_type: str
    is_unread: bool | None = None
    channel: ChannelRef | None = None
    thread_ts: str | None = None
    item_ts: str | None = None


ActivityItemType = Literal[
    "at_user", "at_user_group", "at_channel", "at_everyone", "keyword",
    "channel", "dm", "thread_v2", "message_reaction", "bot_dm_bundle",
    "internal_channel_invite", "external_channel_invite", "external_dm_invite",
    "shared_workspace_invite", "team_joiner_invite", "unjoined_channel_mention",
    "quietly_added_to_channel", "prejoin_dm_welcome_party_alert", "saved_reminder",
    "list_record_assigned", "list_record_edited", "list_user_mentioned",
    "list_todo_notification", "list_approval_request", "list_approval_reviewed",
    "code_review_requested",
]  # fmt: skip


@dataclass(kw_only=True, slots=True)
class UnreadBadgeCountsEvent(BaseEvent):
    TYPE: ClassVar[str] = "unread.badge_counts"
    mode: Literal["activity_v2", "legacy"]
    raw: Any = None
    #: Activity counters keyed by :data:`ActivityItemType` (unknown keys pass through).
    activity: dict[str, int] | None = None
    mention_count: int | None = None
    counts: BadgeCounts | None = None
    alerts: dict[str, int] | None = None


@dataclass(kw_only=True, slots=True)
class LoopDetectedEvent(BaseEvent):
    TYPE: ClassVar[str] = "guard.loop_detected"
    channel_id: str
    reply_count_in_window: int
    thread_ts: str | None = None


@dataclass(kw_only=True, slots=True)
class ProtocolDriftEvent(BaseEvent):
    TYPE: ClassVar[str] = "guard.protocol_drift"
    unknown_type: str
    sample_frame: Any = None
    unknown_subtype: str | None = None


@dataclass(kw_only=True, slots=True)
class OverflowEvent(BaseEvent):
    TYPE: ClassVar[str] = "guard.overflow"
    channel_id: str | None = None
    ts: str | None = None


@dataclass(kw_only=True, slots=True)
class LeaseExpiredEvent(BaseEvent):
    TYPE: ClassVar[str] = "guard.lease_expired"
    target_event_id: str
    handler_id: str
    owner_replica_id: str
    delivery: Literal["at_most_once", "at_least_once"]
    action: Literal["alert_only", "taken_over"]


@dataclass(kw_only=True, slots=True)
class ConnectionReadyEvent(BaseEvent):
    TYPE: ClassVar[str] = "connection.ready"
    fast_reconnect: bool
    degraded: bool


@dataclass(kw_only=True, slots=True)
class ConnectionReconnectingEvent(BaseEvent):
    TYPE: ClassVar[str] = "connection.reconnecting"
    attempt: int
    #: Delay before the next attempt, in milliseconds (schema unit).
    delay_ms: int


@dataclass(kw_only=True, slots=True)
class ConnectionGapEvent(BaseEvent):
    TYPE: ClassVar[str] = "connection.gap"
    cause: Literal["fast_reconnect_reconcile", "cold_resync", "overflow", "state_store_unavailable"]
    recovered: bool
    recovered_count: int
    channels_recovered: list[str] = field(default_factory=list)
    threads_recovered: list[GapThread] = field(default_factory=list)
    truncated: bool = False
    unrecovered: list[GapRange] = field(default_factory=list)
    from_ts: str | None = None
    to_ts: str | None = None


@dataclass(kw_only=True, slots=True)
class AuthRefreshedEvent(BaseEvent):
    TYPE: ClassVar[str] = "auth.refreshed"
    team_id: str
    trigger: Literal["invalid_auth", "token_expired"]
    #: Unix epoch milliseconds (schema unit).
    refreshed_at: int


@dataclass(kw_only=True, slots=True)
class AuthDeadEvent(BaseEvent):
    TYPE: ClassVar[str] = "auth.dead"
    team_id: str
    reason: str


GabrielEvent = (
    MessageCreatedEvent
    | MessageUpdatedEvent
    | MessageDeletedEvent
    | MessageThreadMetaUpdatedEvent
    | MessageOtherEvent
    | AiStreamUpdatedEvent
    | AiStreamCompletedEvent
    | PresenceChangedEvent
    | UserStatusChangedEvent
    | UserDndChangedEvent
    | UserSubteamUpdatedEvent
    | TypingEvent
    | ReactionAddedEvent
    | ReactionRemovedEvent
    | ChannelLifecycleEvent
    | ChannelMarkedEvent
    | UnreadActivityEvent
    | UnreadBadgeCountsEvent
    | ConnectionReadyEvent
    | ConnectionReconnectingEvent
    | ConnectionGapEvent
    | AuthRefreshedEvent
    | AuthDeadEvent
    | LoopDetectedEvent
    | ProtocolDriftEvent
    | OverflowEvent
    | LeaseExpiredEvent
)

#: Event type string → event class.
EVENT_CLASSES: dict[str, type[BaseEvent]] = {
    cls.TYPE: cls
    for cls in (
        MessageCreatedEvent,
        MessageUpdatedEvent,
        MessageDeletedEvent,
        MessageThreadMetaUpdatedEvent,
        MessageOtherEvent,
        AiStreamUpdatedEvent,
        AiStreamCompletedEvent,
        PresenceChangedEvent,
        UserStatusChangedEvent,
        UserDndChangedEvent,
        UserSubteamUpdatedEvent,
        TypingEvent,
        ReactionAddedEvent,
        ReactionRemovedEvent,
        ChannelLifecycleEvent,
        ChannelMarkedEvent,
        UnreadActivityEvent,
        UnreadBadgeCountsEvent,
        ConnectionReadyEvent,
        ConnectionReconnectingEvent,
        ConnectionGapEvent,
        AuthRefreshedEvent,
        AuthDeadEvent,
        LoopDetectedEvent,
        ProtocolDriftEvent,
        OverflowEvent,
        LeaseExpiredEvent,
    )
}

GabrielEventType = str

#: Message-family event types (the created ones are the only ones that advance recovery watermarks).
MESSAGE_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "message.created",
        "message.updated",
        "message.deleted",
        "message.thread_meta_updated",
        "message.other",
        "ai.stream_updated",
        "ai.stream_completed",
    }
)

E = TypeVar("E", bound=BaseEvent)


def event_type_of(selector: str | type[BaseEvent]) -> str:
    """Accepts an event type string or an event class; returns the type string."""
    if isinstance(selector, str):
        if selector not in EVENT_CLASSES:
            from ..errors import GabrielError

            raise GabrielError("INVALID_ARGUMENT", f"unknown event type {selector!r}")
        return selector
    if not selector.TYPE:
        from ..errors import GabrielError

        raise GabrielError("INVALID_ARGUMENT", f"{selector.__name__} is not a concrete event class")
    return selector.TYPE


__all__ = [
    "EVENT_CLASSES",
    "MESSAGE_EVENT_TYPES",
    "ActivityItemType",
    "AiStreamCompletedEvent",
    "AiStreamEvent",
    "AiStreamUpdatedEvent",
    "AttributionReview",
    "AuthDeadEvent",
    "AuthRefreshedEvent",
    "BadgeCounts",
    "BaseEvent",
    "BotProfile",
    "ChannelLifecycleEvent",
    "ChannelMarkedEvent",
    "ChannelRef",
    "ConnectionGapEvent",
    "ConnectionReadyEvent",
    "ConnectionReconnectingEvent",
    "DndInfo",
    "FileRef",
    "GabrielEvent",
    "GabrielEventType",
    "GapRange",
    "GapThread",
    "LeaseExpiredEvent",
    "LoopDetectedEvent",
    "Mention",
    "MentionReason",
    "MessageAttribution",
    "MessageCreatedEvent",
    "MessageDeletedEvent",
    "MessageOrigin",
    "MessageOtherEvent",
    "MessageThreadMetaUpdatedEvent",
    "MessageUpdatedEvent",
    "OverflowEvent",
    "Presence",
    "PresenceChangedEvent",
    "ProtocolDriftEvent",
    "ReactionAddedEvent",
    "ReactionEvent",
    "ReactionRemovedEvent",
    "ReactionTarget",
    "ThreadInfo",
    "TypingEvent",
    "UnreadActivityEvent",
    "UnreadBadgeCountsEvent",
    "UserDndChangedEvent",
    "UserRef",
    "UserStatus",
    "UserStatusChangedEvent",
    "UserSubteamUpdatedEvent",
    "event_type_of",
]
