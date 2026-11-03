"""Stateless mapping from raw WebSocket frames to high-level events.

Each frame yields at most one :class:`Draft`: the event plus the routing / hydration data the
client needs (lane key, thread key, entity IDs to resolve, a ``render`` callback that fills in
names and Markdown once they are known).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, TypeGuard, cast

from ..core.types import RawWsFrame
from ..markdown.attribution import attribution_user_ids, parse_attribution, split_attribution
from ..markdown.blocks_to_markdown import EntityResolver, convert_blocks_to_markdown, mrkdwn_to_markdown
from .types import (
    AiStreamCompletedEvent,
    AiStreamUpdatedEvent,
    BadgeCounts,
    BotProfile,
    ChannelLifecycleEvent,
    ChannelMarkedEvent,
    ChannelRef,
    FileRef,
    GabrielEvent,
    Mention,
    MessageCreatedEvent,
    MessageDeletedEvent,
    MessageOrigin,
    MessageOtherEvent,
    MessageThreadMetaUpdatedEvent,
    MessageUpdatedEvent,
    ReactionAddedEvent,
    ReactionRemovedEvent,
    ReactionTarget,
    ThreadInfo,
    TypingEvent,
    UnreadActivityEvent,
    UnreadBadgeCountsEvent,
    UserRef,
    UserSubteamUpdatedEvent,
)

__all__ = [
    "LIFECYCLE_ACTIONS",
    "MENTION_ACTIVITY_KEYS",
    "MESSAGE_CREATED_SUBTYPES",
    "Draft",
    "EntityNeeds",
    "NormalizeContext",
    "OriginInfo",
    "ReactionTargetKey",
    "badge_snapshot_key",
    "content_hash",
    "default_origin",
    "extract_mentions",
    "lane_key_of",
    "normalize_badge_counts",
    "normalize_frame",
    "render_message_markdown",
    "to_file_refs",
]

Delivery = Literal["realtime", "backfill"]
LifecycleAction = Literal["joined", "left", "im_created", "renamed", "archived"]


@dataclass(frozen=True, slots=True, kw_only=True)
class OriginInfo:
    """Author facts handed to :attr:`NormalizeContext.classify_origin`."""

    ts: str
    content_hash: str
    is_edit: bool
    user: str | None = None
    bot_id: str | None = None
    client_msg_id: str | None = None


@dataclass(slots=True, kw_only=True)
class NormalizeContext:
    """Callbacks the normalizer needs from the stateful client. All optional for pure use."""

    self_user_id: str
    #: Classifies a message author; defaults to the stateless rules (no self-send registry).
    classify_origin: Callable[[OriginInfo], MessageOrigin] | None = None
    #: Whether the current user belongs to the given user group.
    is_my_subteam: Callable[[str], bool] | None = None
    #: Recorded at receive time; defaults to ``"realtime"``.
    delivery: Delivery | None = None


@dataclass(slots=True)
class EntityNeeds:
    """Entity IDs whose names the client should hydrate before rendering."""

    users: set[str] = field(default_factory=set)
    channels: set[str] = field(default_factory=set)
    usergroups: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class ReactionTargetKey:
    """The reacted-to message whose thread root must be looked up."""

    channel: str
    ts: str


def _noop_render(_r: EntityResolver) -> None:
    return None


@dataclass(slots=True, kw_only=True)
class Draft:
    """Internal routing envelope around one high-level event."""

    event: GabrielEvent
    lane_key: str
    needs: EntityNeeds
    #: Whether the event participates in event_id de-duplication.
    dedupe: bool
    #: Re-renders names / Markdown into ``event`` with the current resolver.
    render: Callable[[EntityResolver], None] = _noop_render
    channel_id: str | None = None
    #: Thread root (or own ts) for thread-level executor keys.
    thread_key: str | None = None
    #: Set for message.created only — the ts that advances recovery watermarks.
    created_ts: str | None = None
    #: Reaction drafts whose target thread root must be looked up.
    reaction_target: ReactionTargetKey | None = None


#: Message subtypes that are still a new message (``message.created``).
MESSAGE_CREATED_SUBTYPES: frozenset[str] = frozenset({"file_share", "thread_broadcast", "bot_message", "me_message"})


# --------------------------------------------------------------------------- JS-compatible accessors


def _str(v: Any) -> str | None:
    """A non-empty string, else ``None``."""
    return v if isinstance(v, str) and len(v) > 0 else None


def _obj(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _truthy(v: Any) -> bool:
    """JavaScript truthiness (objects and arrays are truthy even when empty)."""
    if v is None or v is False:
        return False
    if isinstance(v, bool):
        return True
    if isinstance(v, int | float):
        return v != 0 and not (isinstance(v, float) and math.isnan(v))
    if isinstance(v, str):
        return len(v) > 0
    return True


def _is_number(v: Any) -> TypeGuard[int | float]:
    """``typeof v === "number"``."""
    return isinstance(v, int | float) and not isinstance(v, bool)


def _as_int(v: int | float) -> int:
    # JSON integers arrive as int; an integral float is normalized. Other floats are passed
    # through unchanged (the payload keeps the value Slack sent).
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return cast(int, v)


def _num_str(n: int | float) -> str:
    """``String(number)``."""
    if isinstance(n, float):
        if math.isnan(n):
            return "NaN"
        if math.isinf(n):
            return "Infinity" if n > 0 else "-Infinity"
        if n.is_integer() and abs(n) < 1e21:
            return str(int(n))
    return repr(n) if isinstance(n, float) else str(n)


def _js_string(v: Any) -> str:
    """``String(v)`` for JSON values."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if _is_number(v):
        return _num_str(v)
    if v is None:
        return "null"
    if isinstance(v, list):
        return ",".join("" if x is None else _js_string(x) for x in v)
    return "[object Object]"


def _entries(v: Any) -> Iterator[tuple[str, Any]]:
    """``Object.entries(obj(v))``."""
    if isinstance(v, dict):
        yield from v.items()
    elif isinstance(v, list):
        for i, x in enumerate(v):
            yield str(i), x


def _resolve_user(r: EntityResolver, user_id: str) -> str | None:
    return r.user_name(user_id) if r.user_name is not None else None


def _resolve_channel(r: EntityResolver, channel_id: str) -> str | None:
    return r.channel_name(channel_id) if r.channel_name is not None else None


def _resolve_usergroup(r: EntityResolver, subteam_id: str) -> str | None:
    return r.usergroup_handle(subteam_id) if r.usergroup_handle is not None else None


# --------------------------------------------------------------------------- shared helpers


def lane_key_of(frame: RawWsFrame) -> str:
    """Deterministic lane key — depends only on raw frame fields, never on hydration results."""
    channel = frame.get("channel")
    if isinstance(channel, str):
        return channel
    if isinstance(channel, dict) and isinstance(channel.get("id"), str):
        return cast(str, channel["id"])
    item = _obj(frame.get("item"))
    if isinstance(item.get("channel"), str):
        return cast(str, item["channel"])
    subteam = _obj(frame.get("subteam"))
    if isinstance(subteam.get("id"), str):
        return cast(str, subteam["id"])
    if isinstance(frame.get("user"), str):
        return cast(str, frame["user"])
    return "__global__"


def content_hash(message: dict[str, Any]) -> str:
    """Hash of the message body (text, blocks, file IDs, attachments): 32 hex chars.

    Same inputs as the Node SDK; the digest is stable within this SDK (JSON serialization
    details differ between runtimes, so it is not byte-identical to Node's).
    """
    files_raw = message.get("files")
    files = [f.get("id") if isinstance(f, dict) else None for f in files_raw] if isinstance(files_raw, list) else []
    text = message.get("text")
    payload = json.dumps(
        [
            "" if text is None else text,
            message.get("blocks"),
            files,
            message.get("attachments"),
        ],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()[:32]


def default_origin(self_user_id: str, user: str | None, bot_id: str | None) -> MessageOrigin:
    if user is not None and user == self_user_id and bot_id is None:
        return "self_other_client"
    if bot_id is not None:
        return "bot"
    return "other_user"


def to_file_refs(files: Any) -> list[FileRef]:
    if not isinstance(files, list):
        return []
    out: list[FileRef] = []
    for raw in files:
        f = _obj(raw)
        fid = _str(f.get("id"))
        if not fid:
            continue
        ref = FileRef(
            id=fid,
            name=_str(f.get("name")) or fid,
            mime_type=_str(f.get("mimetype")) or "application/octet-stream",
            url_private=_str(f.get("url_private")) or _str(f.get("url_private_download")) or "",
        )
        title = _str(f.get("title"))
        if title:
            ref.title = title
        size = f.get("size")
        if _is_number(size):
            ref.size = _as_int(size)
        permalink = _str(f.get("permalink"))
        if permalink:
            ref.permalink = permalink
        out.append(ref)
    return out


def _walk_elements(node: Any, visit: Callable[[dict[str, Any]], None]) -> None:
    if isinstance(node, list):
        for n in node:
            _walk_elements(n, visit)
        return
    o = _obj(node)
    if len(o) == 0:
        return
    visit(o)
    if isinstance(o.get("elements"), list):
        _walk_elements(o["elements"], visit)


_TEXT_MENTION = re.compile(
    r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>|<!subteam\^(S[A-Z0-9]+)(?:\|[^>]*)?>|<!(here|channel|everyone)(?:\|[^>]*)?>"
)
_TEXT_CHANNEL = re.compile(r"<#([CG][A-Z0-9]+)(?:\|[^>]*)?>")


def extract_mentions(blocks: Any, text: str | None) -> list[Mention]:
    """Extracts mentions from blocks (preferred) or mrkdwn text."""
    seen: set[str] = set()
    out: list[Mention] = []

    def add(kind: Literal["user", "usergroup", "broadcast"], mid: str) -> None:
        k = f"{kind}:{mid}"
        if k not in seen:
            seen.add(k)
            out.append(Mention(kind=kind, id=mid))

    def visit(el: dict[str, Any]) -> None:
        t = el.get("type")
        if t == "user" and isinstance(el.get("user_id"), str):
            add("user", el["user_id"])
        elif t == "usergroup" and isinstance(el.get("usergroup_id"), str):
            add("usergroup", el["usergroup_id"])
        elif t == "broadcast":
            add("broadcast", _str(el.get("range")) or "channel")

    _walk_elements(blocks, visit)
    if len(out) == 0 and text:
        for m in _TEXT_MENTION.finditer(text):
            if m.group(1):
                add("user", m.group(1))
            elif m.group(2):
                add("usergroup", m.group(2))
            elif m.group(3):
                add("broadcast", m.group(3))
    return out


def render_message_markdown(message: dict[str, Any], r: EntityResolver) -> str:
    """Renders a message body to Markdown: blocks first, then mrkdwn text, then legacy attachments.

    A leading attribution block (spec §9.1) is not part of the body.
    """
    md = convert_blocks_to_markdown(split_attribution(message.get("blocks"))[1], r)
    if not md:
        text = message.get("text")
        md = mrkdwn_to_markdown(text if isinstance(text, str) else "", r)
    attachments = message.get("attachments")
    if isinstance(attachments, list):
        parts: list[str] = []
        for raw in attachments:
            a = _obj(raw)
            blocks_md = convert_blocks_to_markdown(a.get("blocks"), r)
            fields = [t for t in (_str(a.get("pretext")), _str(a.get("title")), _str(a.get("text"))) if t]
            body = blocks_md or "\n".join(mrkdwn_to_markdown(t, r) for t in fields) or _str(a.get("fallback"))
            if body:
                parts.append(body)
        if parts:
            md = "\n\n".join(p for p in [md, *parts] if p)
    return md


def _thread_of(message: dict[str, Any], ts: str, subtype: str | None) -> ThreadInfo | None:
    thread_ts = _str(message.get("thread_ts"))
    if not thread_ts:
        return None
    return ThreadInfo(root_ts=thread_ts, is_reply=thread_ts != ts, broadcast=subtype == "thread_broadcast")


def _origin_for(ctx: NormalizeContext, message: dict[str, Any], ts: str, is_edit: bool) -> MessageOrigin:
    user = _str(message.get("user"))
    bot_id = _str(message.get("bot_id"))
    client_msg_id = _str(message.get("client_msg_id"))
    if ctx.classify_origin is not None:
        return ctx.classify_origin(
            OriginInfo(
                user=user,
                bot_id=bot_id,
                client_msg_id=client_msg_id,
                ts=ts,
                content_hash=content_hash(message),
                is_edit=is_edit,
            )
        )
    return default_origin(ctx.self_user_id, user, bot_id)


def _sender_of(message: dict[str, Any]) -> UserRef:
    bot_profile = _obj(message.get("bot_profile"))
    sid = _str(message.get("user")) or _str(bot_profile.get("user_id")) or _str(message.get("bot_id")) or "unknown"
    is_bot = _truthy(message.get("bot_id")) or message.get("subtype") == "bot_message"
    name = _str(bot_profile.get("name")) or _str(message.get("username"))
    return UserRef(id=sid, is_bot=is_bot, name=name, display_name=name)


def _collect_message_needs(needs: EntityNeeds, message: dict[str, Any], mentions: list[Mention]) -> None:
    user = _str(message.get("user"))
    if user and not _truthy(message.get("bot_profile")):
        needs.users.add(user)
    for uid in attribution_user_ids(message):
        needs.users.add(uid)
    for m in mentions:
        if m.kind == "user":
            needs.users.add(m.id)
        if m.kind == "usergroup":
            needs.usergroups.add(m.id)

    def visit(el: dict[str, Any]) -> None:
        if el.get("type") == "channel" and isinstance(el.get("channel_id"), str):
            needs.channels.add(el["channel_id"])

    _walk_elements(message.get("blocks"), visit)
    text = message.get("text")
    if isinstance(text, str):
        for m2 in _TEXT_CHANNEL.finditer(text):
            needs.channels.add(m2.group(1))


def _make_channel_ref(channel_id: str) -> ChannelRef:
    return ChannelRef(id=channel_id)


def _render_channel_name(ref: ChannelRef, channel_id: str, r: EntityResolver) -> None:
    cname = _resolve_channel(r, channel_id)
    if cname:
        ref.name = cname


# --------------------------------------------------------------------------- message family


def _normalize_message_created(frame: RawWsFrame, ctx: NormalizeContext) -> Draft | None:
    channel = _str(frame.get("channel"))
    ts = _str(frame.get("ts"))
    if not channel or not ts:
        return None
    subtype = _str(frame.get("subtype"))
    raw_text = frame.get("text")
    text = raw_text if isinstance(raw_text, str) else ""
    mentions = extract_mentions(split_attribution(frame.get("blocks"))[1], text)
    thread = _thread_of(frame, ts, subtype)
    is_my_subteam = ctx.is_my_subteam if ctx.is_my_subteam is not None else (lambda _id: False)
    is_mentioning_me = any(
        (m.kind == "user" and m.id == ctx.self_user_id)
        or m.kind == "broadcast"
        or (m.kind == "usergroup" and is_my_subteam(m.id))
        for m in mentions
    )
    evt = MessageCreatedEvent(
        event_id=f"{channel}:{ts}:created",
        ts=ts,
        client_msg_id=_str(frame.get("client_msg_id")),
        delivery=ctx.delivery if ctx.delivery is not None else "realtime",
        origin=_origin_for(ctx, frame, ts, False),
        text=text,
        markdown="",
        sender=_sender_of(frame),
        channel=_make_channel_ref(channel),
        thread=thread,
        is_thread_reply=thread is not None and thread.is_reply,
        thread_ts=thread.root_ts if thread is not None else None,
        mentions=mentions,
        is_mentioning_me=is_mentioning_me,
        files=to_file_refs(frame.get("files")),
        subtype=subtype,
        raw=frame,
    )
    needs = EntityNeeds()
    needs.channels.add(channel)
    _collect_message_needs(needs, frame, mentions)

    def render(r: EntityResolver) -> None:
        evt.markdown = render_message_markdown(frame, r)
        attribution = parse_attribution(frame.get("blocks"), r)
        if attribution is not None:
            evt.attribution = attribution
        if not evt.sender.is_bot:
            name = _resolve_user(r, evt.sender.id)
            if name:
                evt.sender.display_name = name
        for m in evt.mentions:
            if m.kind == "user":
                mname = _resolve_user(r, m.id)
            elif m.kind == "usergroup":
                mname = _resolve_usergroup(r, m.id)
            else:
                mname = None
            if mname:
                m.name = mname
        _render_channel_name(evt.channel, channel, r)

    return Draft(
        event=evt,
        lane_key=channel,
        channel_id=channel,
        thread_key=thread.root_ts if thread is not None else ts,
        needs=needs,
        dedupe=True,
        created_ts=ts,
        render=render,
    )


def _normalize_message_changed(frame: RawWsFrame, ctx: NormalizeContext) -> Draft | None:
    channel = _str(frame.get("channel"))
    message = _obj(frame.get("message"))
    previous = _obj(frame["previous_message"]) if "previous_message" in frame else None
    ts = _str(message.get("ts")) or _str(frame.get("ts"))
    if not channel or not ts:
        return None
    event_ts = _str(frame.get("event_ts")) or _str(frame.get("ts")) or ts
    thread_ts = _str(message.get("thread_ts"))
    ai_context = _obj(message.get("ai_context"))
    streaming = _str(message.get("streaming_state"))
    prev_streaming = _str(previous.get("streaming_state")) if previous is not None else None
    is_ai_streaming = (
        streaming is not None
        or prev_streaming == "in_progress"
        or ai_context.get("result_status") == "streaming"
        or ai_context.get("result_status") == "complete"
    )
    needs = EntityNeeds()
    needs.channels.add(channel)

    if is_ai_streaming:
        in_progress = streaming == "in_progress" or ai_context.get("result_status") == "streaming"
        bot = _obj(message.get("bot_profile"))
        bot_id = _str(bot.get("id")) or _str(message.get("bot_id"))
        ai_cls = AiStreamUpdatedEvent if in_progress else AiStreamCompletedEvent
        kind = "ai_stream_updated" if in_progress else "ai_stream_completed"
        ai_evt = ai_cls(
            event_id=f"{channel}:{ts}:{kind}:{event_ts}",
            ts=ts,
            channel=_make_channel_ref(channel),
            bot_profile=(
                BotProfile(id=bot_id, name=_str(bot.get("name")), app_id=_str(bot.get("app_id"))) if bot_id else None
            ),
            streaming_state="in_progress" if in_progress else "completed",
            markdown="",
            thread_ts=thread_ts,
        )

        def render_ai(r: EntityResolver) -> None:
            ai_evt.markdown = render_message_markdown(message, r)
            _render_channel_name(ai_evt.channel, channel, r)

        return Draft(
            event=ai_evt,
            lane_key=channel,
            channel_id=channel,
            thread_key=thread_ts or ts,
            needs=needs,
            dedupe=True,
            render=render_ai,
        )

    # Metadata-only change (reply_count, unfurl bookkeeping…): body hash unchanged → no event.
    if previous is not None and content_hash(previous) == content_hash(message):
        return None

    msg_text = message.get("text")
    mentions = extract_mentions(
        split_attribution(message.get("blocks"))[1], msg_text if isinstance(msg_text, str) else None
    )
    _collect_message_needs(needs, message, mentions)
    edited = _obj(message.get("edited"))
    evt = MessageUpdatedEvent(
        event_id=f"{channel}:{ts}:updated:{event_ts}",
        ts=ts,
        channel=_make_channel_ref(channel),
        sender=_sender_of(message),
        origin=_origin_for(ctx, message, ts, True),
        current_markdown="",
        edited_at=_str(edited.get("ts")) or event_ts,
        thread_ts=thread_ts,
        raw=frame,
    )
    if previous is not None:
        prev_mentions = extract_mentions(split_attribution(previous.get("blocks"))[1], _str(previous.get("text")))
        _collect_message_needs(needs, previous, prev_mentions)

    def render(r: EntityResolver) -> None:
        evt.current_markdown = render_message_markdown(message, r)
        if previous is not None:
            evt.previous_markdown = render_message_markdown(previous, r)
        attribution = parse_attribution(message.get("blocks"), r)
        if attribution is not None:
            evt.attribution = attribution
        name = None if evt.sender.is_bot else _resolve_user(r, evt.sender.id)
        if name:
            evt.sender.display_name = name
        _render_channel_name(evt.channel, channel, r)

    return Draft(
        event=evt,
        lane_key=channel,
        channel_id=channel,
        thread_key=thread_ts or ts,
        needs=needs,
        dedupe=True,
        render=render,
    )


def _normalize_message_deleted(frame: RawWsFrame) -> Draft | None:
    channel = _str(frame.get("channel"))
    deleted_ts = _str(frame.get("deleted_ts")) or _str(frame.get("ts"))
    if not channel or not deleted_ts:
        return None
    event_ts = _str(frame.get("event_ts")) or _str(frame.get("ts")) or deleted_ts
    previous = frame.get("previous_message")
    thread_ts = _str(_obj(previous).get("thread_ts"))
    evt = MessageDeletedEvent(
        event_id=f"{channel}:{deleted_ts}:deleted:{event_ts}",
        deleted_ts=deleted_ts,
        channel=_make_channel_ref(channel),
        previous_message=previous,
        thread_ts=thread_ts,
    )
    needs = EntityNeeds()
    needs.channels.add(channel)
    return Draft(
        event=evt,
        lane_key=channel,
        channel_id=channel,
        thread_key=thread_ts or deleted_ts,
        needs=needs,
        dedupe=True,
        render=lambda r: _render_channel_name(evt.channel, channel, r),
    )


def _normalize_message_replied(frame: RawWsFrame) -> Draft | None:
    channel = _str(frame.get("channel"))
    parent = _obj(frame.get("message"))
    root = _str(parent.get("thread_ts")) or _str(parent.get("ts")) or _str(frame.get("ts"))
    if not channel or not root:
        return None
    latest = _str(parent.get("latest_reply")) or ""
    reply_count = parent.get("reply_count")
    evt = MessageThreadMetaUpdatedEvent(
        event_id=f"{channel}:{root}:thread_meta_updated:{_str(frame.get('event_ts')) or latest}",
        channel=_make_channel_ref(channel),
        thread_root_ts=root,
        reply_count=_as_int(reply_count) if _is_number(reply_count) else 0,
        latest_reply_ts=latest,
    )
    needs = EntityNeeds()
    needs.channels.add(channel)
    return Draft(
        event=evt,
        lane_key=channel,
        channel_id=channel,
        thread_key=root,
        needs=needs,
        dedupe=True,
        render=lambda r: _render_channel_name(evt.channel, channel, r),
    )


def _normalize_message_other(frame: RawWsFrame, subtype: str) -> Draft:
    channel = _str(frame.get("channel"))
    ts = _str(frame.get("ts"))
    evt = MessageOtherEvent(
        event_id=f"{channel or '-'}:{ts or '-'}:other_{subtype}:{_str(frame.get('event_ts')) or ts or '-'}",
        raw_subtype=subtype,
        channel=_make_channel_ref(channel) if channel else None,
        ts=ts,
        raw_frame=frame,
    )
    needs = EntityNeeds()
    if channel:
        needs.channels.add(channel)

    def render(r: EntityResolver) -> None:
        if channel and evt.channel is not None:
            _render_channel_name(evt.channel, channel, r)

    return Draft(
        event=evt,
        lane_key=lane_key_of(frame),
        channel_id=channel,
        thread_key=(_str(frame.get("thread_ts")) or ts) if ts else None,
        needs=needs,
        dedupe=ts is not None,
        render=render,
    )


# --------------------------------------------------------------------------- interactions


def _normalize_typing(frame: RawWsFrame) -> Draft | None:
    channel = _str(frame.get("channel"))
    user = _str(frame.get("user"))
    if not channel or not user:
        return None
    thread_ts = _str(frame.get("thread_ts"))
    frame_id = frame.get("id")
    evt = TypingEvent(
        event_id=f"{channel}:typing:{user}:{_num_str(frame_id) if _is_number(frame_id) else '-'}",
        user=UserRef(id=user),
        channel=ChannelRef(id=channel),
        thread_root_ts=thread_ts,
    )
    needs = EntityNeeds()
    needs.users.add(user)
    needs.channels.add(channel)

    def render(r: EntityResolver) -> None:
        display_name = _resolve_user(r, user)
        if display_name:
            evt.user.display_name = display_name
        _render_channel_name(evt.channel, channel, r)

    return Draft(
        event=evt,
        lane_key=channel,
        channel_id=channel,
        thread_key=thread_ts,
        needs=needs,
        dedupe=False,
        render=render,
    )


def _normalize_reaction(frame: RawWsFrame, added: bool) -> Draft | None:
    user = _str(frame.get("user"))
    reaction = _str(frame.get("reaction"))
    item = _obj(frame.get("item"))
    if not user or not reaction:
        return None
    item_type = _str(item.get("type"))
    channel = _str(item.get("channel")) or ""
    ts = _str(item.get("ts"))
    file_id = _str(item.get("file")) or _str(item.get("file_comment"))
    kind = "reaction_added" if added else "reaction_removed"
    event_ts = _str(frame.get("event_ts")) or _str(frame.get("ts")) or "-"
    item_user = _str(frame.get("item_user"))
    cls = ReactionAddedEvent if added else ReactionRemovedEvent
    evt = cls(
        event_id=f"{channel or file_id or '-'}:{ts or file_id or '-'}:{kind}:{event_ts}:{user}:{reaction}",
        reaction=reaction,
        user=UserRef(id=user),
        channel=ChannelRef(id=channel),
        target=ReactionTarget(
            type="message" if item_type == "message" else "file",
            ts=ts,
            file_id=file_id if item_type != "message" and file_id else None,
        ),
        item_user_id=item_user,
    )
    needs = EntityNeeds()
    needs.users.add(user)
    if channel:
        needs.channels.add(channel)

    def render(r: EntityResolver) -> None:
        display_name = _resolve_user(r, user)
        if display_name:
            evt.user.display_name = display_name
        if channel:
            _render_channel_name(evt.channel, channel, r)

    return Draft(
        event=evt,
        lane_key=channel or lane_key_of(frame),
        channel_id=channel or None,
        thread_key=ts,
        needs=needs,
        dedupe=True,
        reaction_target=ReactionTargetKey(channel, ts) if channel and ts and item_type == "message" else None,
        render=render,
    )


# --------------------------------------------------------------------------- read state


def _normalize_marked(frame: RawWsFrame) -> Draft | None:
    channel = _str(frame.get("channel"))
    ts = _str(frame.get("ts"))
    if not channel or not ts:
        return None

    def num(*vals: Any) -> int:
        for v in vals:
            if _is_number(v):
                return _as_int(v)
        return 0

    evt = ChannelMarkedEvent(
        event_id=f"{channel}:{ts}:marked:{_str(frame.get('event_ts')) or ts}",
        channel=_make_channel_ref(channel),
        last_read_ts=ts,
        unread_count=num(frame.get("unread_count_display"), frame.get("unread_count")),
        mention_count=num(
            frame.get("mention_count_display"),
            frame.get("mention_count"),
            frame.get("num_mentions_display"),
            frame.get("num_mentions"),
        ),
    )
    needs = EntityNeeds()
    needs.channels.add(channel)
    return Draft(
        event=evt,
        lane_key=channel,
        channel_id=channel,
        needs=needs,
        dedupe=False,
        render=lambda r: _render_channel_name(evt.channel, channel, r),
    )


def _normalize_activity(frame: RawWsFrame) -> Draft | None:
    entry = _obj(frame.get("entry"))
    item = _obj(entry.get("item"))
    message = _obj(item.get("message"))
    activity_type = _str(item.get("type"))
    if not activity_type:
        return None
    channel = _str(message.get("channel")) or _str(item.get("channel"))
    thread_ts = _str(message.get("thread_ts")) or _str(item.get("thread_ts"))
    item_ts = _str(message.get("ts")) or _str(entry.get("feed_ts"))
    key = _str(entry.get("key")) or _str(frame.get("key")) or activity_type
    is_unread = entry.get("is_unread")
    evt = UnreadActivityEvent(
        event_id=f"activity:{key}:{_str(frame.get('event_ts')) or '-'}",
        activity_type=activity_type,
        is_unread=is_unread if isinstance(is_unread, bool) else None,
        channel=_make_channel_ref(channel) if channel else None,
        thread_ts=thread_ts,
        item_ts=item_ts,
    )
    needs = EntityNeeds()
    if channel:
        needs.channels.add(channel)

    def render(r: EntityResolver) -> None:
        if channel and evt.channel is not None:
            _render_channel_name(evt.channel, channel, r)

    return Draft(
        event=evt,
        lane_key=channel or "__global__",
        channel_id=channel,
        needs=needs,
        dedupe=False,
        render=render,
    )


#: Mention roll-up used by the official client (``sumMentionCounts``).
MENTION_ACTIVITY_KEYS: tuple[str, ...] = (
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
)


def normalize_badge_counts(frame: RawWsFrame) -> Draft | None:
    event_ts = _str(frame.get("event_ts")) or "-"
    activity_v2 = frame.get("activity_v2")
    if _truthy(activity_v2) and isinstance(activity_v2, dict | list):
        activity: dict[str, int] = {k: _as_int(v) for k, v in _entries(activity_v2) if _is_number(v)}
        mention_count = sum(activity.get(k, 0) for k in MENTION_ACTIVITY_KEYS)
        evt = UnreadBadgeCountsEvent(
            event_id=f"badge_counts:{event_ts}",
            mode="activity_v2",
            activity=activity,
            mention_count=mention_count,
            raw=frame,
        )
    else:
        c = _obj(frame.get("counts"))

        def n(v: Any) -> int:
            return _as_int(v) if _is_number(v) else 0

        alerts: dict[str, int] = {k: _as_int(v) for k, v in _entries(frame.get("alerts")) if _is_number(v)}
        evt = UnreadBadgeCountsEvent(
            event_id=f"badge_counts:{event_ts}",
            mode="legacy",
            counts=BadgeCounts(
                dms=n(c.get("dms")),
                app_dms=n(c.get("app_dms")),
                channels=n(c.get("channels")),
                thread_mentions=n(c.get("thread_mentions")),
                thread_unreads=n(c.get("thread_unreads")),
            ),
            alerts=alerts if alerts else None,
            raw=frame,
        )
    return Draft(event=evt, lane_key="__global__", needs=EntityNeeds(), dedupe=False)


def badge_snapshot_key(evt: UnreadBadgeCountsEvent) -> str:
    """Content key for badge snapshots: identical snapshots pushed back-to-back are dropped."""

    def sorted_pairs(o: dict[str, int] | None) -> str:
        return ",".join(f"{k}={_num_str(o[k])}" for k in sorted(o)) if o is not None else ""

    counts = json.dumps(evt.counts.to_dict(), separators=(",", ":")) if evt.counts is not None else ""
    return f"{evt.mode}|{sorted_pairs(evt.activity)}|{counts}|{sorted_pairs(evt.alerts)}"


# --------------------------------------------------------------------------- lifecycle / subteams

#: Raw frame type → ``channel.lifecycle`` action (membership frames are handled separately).
LIFECYCLE_ACTIONS: dict[str, LifecycleAction] = {
    "channel_joined": "joined",
    "group_joined": "joined",
    "channel_left": "left",
    "group_left": "left",
    "im_created": "im_created",
    "channel_rename": "renamed",
    "group_rename": "renamed",
    "channel_archive": "archived",
    "group_archive": "archived",
}


def _normalize_lifecycle(frame: RawWsFrame, ctx: NormalizeContext) -> Draft | None:
    raw_type = frame.get("type")
    ftype = raw_type if isinstance(raw_type, str) else ""
    action: LifecycleAction | None = LIFECYCLE_ACTIONS.get(ftype)
    if ftype in ("member_joined_channel", "member_left_channel"):
        if frame.get("user") != ctx.self_user_id:
            return None
        action = "joined" if ftype == "member_joined_channel" else "left"
    if not action:
        return None
    ch_obj = _obj(frame.get("channel"))
    raw_channel = frame.get("channel")
    cid = raw_channel if isinstance(raw_channel, str) else _str(ch_obj.get("id"))
    if not cid:
        return None
    channel = ChannelRef(id=cid)
    name = _str(ch_obj.get("name"))
    if name:
        channel.name = name
    if isinstance(ch_obj.get("is_private"), bool):
        channel.is_private = ch_obj["is_private"]
    if ftype == "im_created":
        channel.is_im = True
    if isinstance(ch_obj.get("is_mpim"), bool):
        channel.is_mpim = ch_obj["is_mpim"]
    evt = ChannelLifecycleEvent(
        event_id=f"{cid}:lifecycle:{action}:{_str(frame.get('event_ts')) or '-'}",
        action=action,
        channel=channel,
    )
    needs = EntityNeeds()
    if not name and action != "im_created":
        needs.channels.add(cid)

    def render(r: EntityResolver) -> None:
        if not evt.channel.name:
            _render_channel_name(evt.channel, cid, r)

    return Draft(event=evt, lane_key=cid, channel_id=cid, needs=needs, dedupe=False, render=render)


def _normalize_subteam_updated(frame: RawWsFrame) -> Draft | None:
    subteam = _obj(frame.get("subteam"))
    sid = _str(subteam.get("id"))
    if not sid:
        return None
    date_update = subteam.get("date_update")
    fallback_ts = "-" if date_update is None else date_update
    users = subteam.get("users")
    evt = UserSubteamUpdatedEvent(
        event_id=f"subteam:{sid}:{_str(frame.get('event_ts')) or _js_string(fallback_ts)}",
        subteam_id=sid,
        handle=_str(subteam.get("handle")) or "",
        member_ids=[u for u in users if isinstance(u, str)] if isinstance(users, list) else [],
    )
    return Draft(event=evt, lane_key=sid, needs=EntityNeeds(), dedupe=False)


# --------------------------------------------------------------------------- entry point


def normalize_frame(frame: RawWsFrame, ctx: NormalizeContext) -> Draft | None:
    """Stateless mapping from one raw frame to at most one primary high-level event.

    "One primary event per frame". Stateful domains (presence, profile/DND refetch,
    member-diff subteam updates) are produced by the client itself.
    """
    match frame.get("type"):
        case "message":
            subtype = _str(frame.get("subtype"))
            if not subtype or subtype in MESSAGE_CREATED_SUBTYPES:
                return _normalize_message_created(frame, ctx)
            if subtype == "message_changed":
                return _normalize_message_changed(frame, ctx)
            if subtype == "message_deleted":
                return _normalize_message_deleted(frame)
            if subtype == "message_replied":
                return _normalize_message_replied(frame)
            return _normalize_message_other(frame, subtype)
        case "user_typing":
            return _normalize_typing(frame)
        case "reaction_added":
            return _normalize_reaction(frame, True)
        case "reaction_removed":
            return _normalize_reaction(frame, False)
        case "channel_marked" | "im_marked" | "group_marked" | "mpim_marked":
            return _normalize_marked(frame)
        case "activity":
            return _normalize_activity(frame)
        case "badge_counts_updated":
            return normalize_badge_counts(frame)
        case "subteam_updated" | "subteam_created":
            return _normalize_subteam_updated(frame)
        case _:
            return _normalize_lifecycle(frame, ctx)
