"""Domain APIs: threads, search, reactions, users, channels and files."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeVar, overload

from ..core.types import RequestOpts
from ..errors import GabrielError
from ..events.normalize import render_message_markdown, to_file_refs
from ..events.types import ChannelRef, DndInfo, FileRef, UserRef
from ..util.validate import (
    assert_channel_id,
    assert_non_empty_string,
    assert_ts,
    assert_user_id,
    is_ts,
    normalize_emoji,
)
from .hydrator import Hydrator, channel_ref, to_user_record, user_ref

if TYPE_CHECKING:
    from ..core.core import GabrielCore

Obj = dict[str, Any]
T = TypeVar("T")


def _s(v: Any) -> str | None:
    return v if isinstance(v, str) and len(v) > 0 else None


def _o(v: Any) -> Obj:
    return v if isinstance(v, dict) else {}


#: Highlight delimiters used by search results.
_HIGHLIGHT = re.compile("[]")
_USER_MENTION = re.compile(r"<@([UW][A-Z0-9]+)")
_HUMAN_ID = re.compile(r"^[UW]")


def strip_highlight(text: str) -> str:
    return _HIGHLIGHT.sub("", text)


async def _sender_of(h: Hydrator, msg: Obj) -> UserRef:
    bot = _o(msg.get("bot_profile"))
    id = _s(msg.get("user")) or _s(bot.get("user_id")) or _s(msg.get("bot_id")) or "unknown"
    bot_name = _s(bot.get("name"))
    if bot_name and not _s(msg.get("user")):
        return UserRef(id=id, name=bot_name, display_name=bot_name, is_bot=True)
    if not _HUMAN_ID.match(id):
        return UserRef(id=id, is_bot=True)
    try:
        rec = await h.users.load(id)
    except Exception:
        rec = None
    return user_ref(rec, id)


@dataclass(slots=True)
class _Rendered:
    msg: Obj
    sender: UserRef
    markdown: str
    files: list[FileRef]


async def _render_messages(h: Hydrator, messages: list[Obj]) -> list[_Rendered]:
    """Loads every user referenced by a list of messages, then renders Markdown."""
    ids: dict[str, None] = {}
    for m in messages:
        if _s(m.get("user")):
            ids[m["user"]] = None
        for mm in _USER_MENTION.finditer(_s(m.get("text")) or ""):
            ids[mm.group(1)] = None
    await asyncio.gather(*(h.users.load(id) for id in ids), return_exceptions=True)

    async def one(msg: Obj) -> _Rendered:
        return _Rendered(
            msg, await _sender_of(h, msg), render_message_markdown(msg, h.resolver), to_file_refs(msg.get("files"))
        )

    return list(await asyncio.gather(*(one(m) for m in messages)))


# ---------------------------------------------------------------------------------------------
# threads


@dataclass(kw_only=True, slots=True)
class ThreadMessage:
    ts: str
    sender: UserRef
    markdown: str
    files: list[FileRef]
    is_deleted: bool


@dataclass(kw_only=True, slots=True)
class ThreadContext:
    channel: ChannelRef
    root_ts: str
    messages: list[ThreadMessage]
    participants: list[UserRef]
    #: Readable transcript with resolved names, ready to use as LLM input.
    formatted_transcript: str


def _format_time(ts: str) -> str:
    d = datetime.fromtimestamp(int(ts.split(".")[0]), tz=UTC)
    return d.strftime("%Y-%m-%d %H:%M:%SZ")


def format_transcript(channel: ChannelRef, root_ts: str, messages: list[ThreadMessage]) -> str:
    where = f"direct message {channel.name or channel.id}" if channel.is_im else f"#{channel.name or channel.id}"
    lines = [f"Thread in {where} (root {root_ts}), {len(messages)} message(s):", ""]
    for m in messages:
        who = m.sender.display_name or m.sender.name or m.sender.id
        handle = f" (@{m.sender.handle})" if m.sender.handle else ""
        if m.is_deleted:
            body = "_(message deleted)_"
        else:
            body = m.markdown or ("" if m.files else "_(empty)_")
        files = f"\n  [files: {', '.join(f.name for f in m.files)}]" if m.files else ""
        lines.append(f"[{_format_time(m.ts)}] {who}{handle}:")
        lines.extend(f"  {line}" for line in body.split("\n"))
        if files:
            lines.append(files)
        lines.append("")
    return "\n".join(lines).rstrip()


class ThreadsApi:
    def __init__(
        self,
        core: GabrielCore,
        h: Hydrator,
        on_watch: Callable[[str, str], None],
        on_unwatch: Callable[[str, str], None],
    ) -> None:
        self._core = core
        self._h = h
        self._on_watch = on_watch
        self._on_unwatch = on_unwatch

    async def _page(
        self, channel: str, ts: str, limit: int, cursor: str | None = None, oldest: str | None = None
    ) -> Obj:
        fields: Obj = {
            "channel": channel,
            "ts": ts,
            "inclusive": True,
            "limit": limit,
            "include_tombstones": True,
            "cached_latest_updates": "{}",
        }
        if cursor:
            fields["cursor"] = cursor
        if oldest:
            fields["oldest"] = oldest
        return await self._core.call_web_api(
            "conversations.replies", fields, RequestOpts(reason="history-api/fetchReplies")
        )

    async def get_context(self, channel: str, thread_ts: str, *, limit: int = 50) -> ThreadContext:
        """Whole-thread context. ``thread_ts`` may be the root or any reply; the root is located automatically."""
        channel = assert_channel_id(channel)
        ts = assert_ts(thread_ts, "thread_ts")
        limit = min(max(1, limit), 1000)
        raw: list[Obj] = []
        cursor: str | None = None
        while True:
            res = await self._page(channel, ts, min(200, limit - len(raw)), cursor)
            raw.extend(m for m in res.get("messages") or [] if isinstance(m, dict))
            cursor = _s(_o(res.get("response_metadata")).get("next_cursor")) if res.get("has_more") else None
            if not (cursor and len(raw) < limit):
                break
        first = raw[0] if raw else None
        root_ts = ((_s(first.get("thread_ts")) or _s(first.get("ts"))) if first else None) or ts
        for raw_msg in raw:
            if _s(raw_msg.get("ts")):
                self._h.record_thread_root(raw_msg["ts"], _s(raw_msg.get("thread_ts")))
        rendered = await _render_messages(self._h, raw)
        messages = [
            ThreadMessage(
                ts=_s(r.msg.get("ts")) or "",
                sender=r.sender,
                markdown=r.markdown,
                files=r.files,
                is_deleted=r.msg.get("subtype") == "tombstone" or r.msg.get("is_deleted") is True,
            )
            for r in rendered
        ]
        participants: dict[str, UserRef] = {}
        for m in messages:
            participants.setdefault(m.sender.id, m.sender)
        try:
            ch = await self._h.load_channel(channel)
        except Exception:
            ch = None
        ch_ref = channel_ref(ch, channel)
        if ch is not None and ch.im_user_id:
            im_name = self._h.resolver.channel_name(channel) if self._h.resolver.channel_name else None
            if im_name:
                ch_ref.name = im_name
        return ThreadContext(
            channel=ch_ref,
            root_ts=root_ts,
            messages=messages,
            participants=list(participants.values()),
            formatted_transcript=format_transcript(ch_ref, root_ts, messages),
        )

    async def iterate_replies(
        self, channel: str, thread_ts: str, *, page_size: int = 100, oldest: str | None = None
    ) -> AsyncIterator[ThreadMessage]:
        channel = assert_channel_id(channel)
        ts = assert_ts(thread_ts, "thread_ts")
        page_size = min(max(1, page_size), 200)
        cursor: str | None = None
        while True:
            res = await self._page(channel, ts, page_size, cursor, oldest)
            msgs = [m for m in res.get("messages") or [] if isinstance(m, dict)]
            for r in await _render_messages(self._h, msgs):
                yield ThreadMessage(
                    ts=_s(r.msg.get("ts")) or "",
                    sender=r.sender,
                    markdown=r.markdown,
                    files=r.files,
                    is_deleted=r.msg.get("subtype") == "tombstone",
                )
            cursor = _s(_o(res.get("response_metadata")).get("next_cursor")) if res.get("has_more") else None
            if not cursor:
                return

    async def mark_read(self, channel: str, thread_ts: str, last_read_ts: str) -> None:
        await self._core.call_web_api(
            "subscriptions.thread.mark",
            {
                "channel": assert_channel_id(channel),
                "thread_ts": assert_ts(thread_ts, "thread_ts"),
                "ts": assert_ts(last_read_ts, "last_read_ts"),
                "read": 1,
            },
            RequestOpts(reason="marking-thread"),
        )

    async def subscribe(self, channel: str, thread_ts: str) -> None:
        channel = assert_channel_id(channel)
        thread_ts = assert_ts(thread_ts, "thread_ts")
        await self._core.call_web_api(
            "subscriptions.thread.add",
            {"channel": channel, "thread_ts": thread_ts},
            RequestOpts(reason="thread-subscribe"),
        )
        self._on_watch(channel, thread_ts)

    async def unsubscribe(self, channel: str, thread_ts: str) -> None:
        channel = assert_channel_id(channel)
        thread_ts = assert_ts(thread_ts, "thread_ts")
        await self._core.call_web_api(
            "subscriptions.thread.remove",
            {"channel": channel, "thread_ts": thread_ts},
            RequestOpts(reason="thread-unsubscribe"),
        )
        self._on_unwatch(channel, thread_ts)


# ---------------------------------------------------------------------------------------------
# files


@dataclass(kw_only=True, slots=True)
class FileInfo(FileRef):
    created_ts: str | None = None
    shared_in: list[str] | None = None


def to_file_info(raw: Obj) -> FileInfo:
    refs = to_file_refs([raw])
    if refs:
        ref = refs[0]
        info = FileInfo(
            id=ref.id,
            name=ref.name,
            title=ref.title,
            mime_type=ref.mime_type,
            size=ref.size,
            url_private=ref.url_private,
            permalink=ref.permalink,
        )
    else:
        info = FileInfo(id=str(raw.get("id") or ""), name="", mime_type="application/octet-stream", url_private="")
    created = raw.get("created")
    if isinstance(created, int | float) and not isinstance(created, bool):
        info.created_ts = str(int(created)) if float(created).is_integer() else str(created)
    shared = [
        x
        for v in (raw.get("channels"), raw.get("groups"), raw.get("ims"))
        if isinstance(v, list)
        for x in v
        if isinstance(x, str)
    ]
    if shared:
        info.shared_in = shared
    return info


# FilesApi lives in files.py (it needs the authenticated HTTP transport).

# ---------------------------------------------------------------------------------------------
# search

SearchSort = Literal["timestamp", "score"]


@dataclass(kw_only=True, slots=True)
class SearchPage(Generic[T]):
    items: list[T]
    page: int
    has_more: bool
    total_count: int | None = None


@dataclass(kw_only=True, slots=True)
class SearchMessageHit:
    ts: str
    channel: ChannelRef
    sender: UserRef
    markdown: str
    thread_ts: str | None = None
    permalink: str | None = None


@dataclass(slots=True)
class _ModuleResult:
    items: list[Obj]
    page: int
    has_more: bool
    total: int | None = None


class SearchApi:
    def __init__(self, core: GabrielCore, h: Hydrator) -> None:
        self._core = core
        self._h = h

    async def _module(self, module: str, query: str, count: int, page: int, sort: SearchSort) -> _ModuleResult:
        q = assert_non_empty_string(query, "query", 1000)
        page = max(1, int(page))
        count = min(100, max(1, int(count)))
        res = await self._core.call_web_api(
            f"search.modules.{module}",
            {
                "module": module,
                "query": q,
                "page": page,
                "count": count,
                "sort": sort,
                "sort_dir": "desc",
                "extracts": 1,
                "highlight": 1,
                "max_extract_len": 200,
                "extra_message_data": 1,
                "no_user_profile": 1,
                "include_files_shares": 1,
                "search_context": f"desktop_{module}_tab",
            },
            RequestOpts(reason="search-api/fetchSearchModule"),
        )
        p = _o(res.get("pagination"))
        items = [i for i in res.get("items") or [] if isinstance(i, dict)]
        page_count = p.get("page_count")
        has_more = page < page_count if isinstance(page_count, int) else len(items) >= count
        total = p.get("total_count")
        return _ModuleResult(items, page, has_more, total if isinstance(total, int) else None)

    async def messages(
        self, query: str, *, count: int = 20, page: int = 1, sort: SearchSort = "timestamp"
    ) -> SearchPage[SearchMessageHit]:
        r = await self._module("messages", query, count, page, sort)
        flat: list[tuple[Obj, Obj]] = []
        for item in r.items:
            channel = _o(item.get("channel"))
            msgs = item["messages"] if isinstance(item.get("messages"), list) else [item]
            for m in msgs:
                if isinstance(m, dict):
                    flat.append((m, channel if channel else _o(m.get("channel"))))
        rendered = await _render_messages(
            self._h, [{**m, "text": strip_highlight(_s(m.get("text")) or "")} for m, _ in flat]
        )
        hits: list[SearchMessageHit] = []
        for (_, ch), r2 in zip(flat, rendered, strict=True):
            ts = _s(r2.msg.get("ts"))
            if not ts:
                continue
            ch_name = _s(ch.get("name"))
            hit = SearchMessageHit(
                ts=ts,
                channel=ChannelRef(
                    id=_s(ch.get("id")) or "",
                    name=strip_highlight(ch_name) if ch_name else None,
                    is_private=ch["is_private"] if isinstance(ch.get("is_private"), bool) else None,
                ),
                sender=r2.sender,
                markdown=strip_highlight(r2.markdown),
                thread_ts=_s(r2.msg.get("thread_ts")),
                permalink=_s(r2.msg.get("permalink")),
            )
            hits.append(hit)
        return SearchPage(items=hits, page=r.page, has_more=r.has_more, total_count=r.total)

    async def files(
        self, query: str, *, count: int = 20, page: int = 1, sort: SearchSort = "timestamp"
    ) -> SearchPage[FileInfo]:
        r = await self._module("files", query, count, page, sort)
        items: list[FileInfo] = []
        for it in r.items:
            info = to_file_info(it)
            info.name = strip_highlight(info.name)
            if info.title:
                info.title = strip_highlight(info.title)
            items.append(info)
        return SearchPage(items=items, page=r.page, has_more=r.has_more, total_count=r.total)

    async def channels(
        self, query: str, *, count: int = 20, page: int = 1, sort: SearchSort = "timestamp"
    ) -> SearchPage[ChannelRef]:
        r = await self._module("channels", query, count, page, sort)
        items: list[ChannelRef] = []
        for c in r.items:
            ref = ChannelRef(id=_s(c.get("id")) or "")
            if _s(c.get("name")):
                ref.name = strip_highlight(c["name"])
            if isinstance(c.get("is_private"), bool):
                ref.is_private = c["is_private"]
            if isinstance(c.get("is_mpim"), bool):
                ref.is_mpim = c["is_mpim"]
            items.append(ref)
        return SearchPage(items=items, page=r.page, has_more=r.has_more, total_count=r.total)

    async def people(
        self, query: str, *, count: int = 20, page: int = 1, sort: SearchSort = "timestamp"
    ) -> SearchPage[UserRef]:
        r = await self._module("people", query, count, page, sort)
        team_id = self._core.session.team_id
        items = [
            user_ref(
                to_user_record({**u, "name": strip_highlight(_s(u.get("name")) or "") or None}, team_id),
                _s(u.get("id")) or "",
            )
            for u in r.items
        ]
        return SearchPage(items=items, page=r.page, has_more=r.has_more, total_count=r.total)

    async def iterate_messages(
        self, query: str, *, count: int = 20, sort: SearchSort = "timestamp", max_pages: int = 50
    ) -> AsyncIterator[SearchMessageHit]:
        """Paged iterator de-duplicated by ts. Search pagination is not a snapshot: results may shift."""
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            res = await self.messages(query, count=count, page=page, sort=sort)
            for hit in res.items:
                key = f"{hit.channel.id}:{hit.ts}"
                if key in seen:
                    continue
                seen.add(key)
                yield hit
            if not res.has_more:
                return

    async def iterate_files(
        self, query: str, *, count: int = 20, sort: SearchSort = "timestamp", max_pages: int = 50
    ) -> AsyncIterator[FileInfo]:
        seen: set[str] = set()
        for page in range(1, max_pages + 1):
            res = await self.files(query, count=count, page=page, sort=sort)
            for f in res.items:
                if f.id in seen:
                    continue
                seen.add(f.id)
                yield f
            if not res.has_more:
                return


# ---------------------------------------------------------------------------------------------
# reactions


class ReactionsApi:
    def __init__(self, core: GabrielCore) -> None:
        self._core = core

    async def add(self, channel: str, ts: str, emoji: str) -> None:
        """Idempotent: ``already_reacted`` is treated as success."""
        await self._call("reactions.add", channel, ts, emoji, "already_reacted")

    async def remove(self, channel: str, ts: str, emoji: str) -> None:
        """Idempotent: ``no_reaction`` is treated as success."""
        await self._call("reactions.remove", channel, ts, emoji, "no_reaction")

    async def _call(self, method: str, channel: str, ts: str, emoji: str, benign: str) -> None:
        try:
            await self._core.call_web_api(
                method,
                {"channel": assert_channel_id(channel), "timestamp": assert_ts(ts), "name": normalize_emoji(emoji)},
                RequestOpts(reason="changeReactionFromUserAction"),
            )
        except GabrielError as err:
            if err.raw_slack_error == benign:
                return
            raise


# ---------------------------------------------------------------------------------------------
# users & channels


class UsersApi:
    def __init__(self, h: Hydrator) -> None:
        self._h = h

    @overload
    async def resolve(self, ids: str) -> UserRef | None: ...
    @overload
    async def resolve(self, ids: list[str]) -> list[UserRef | None]: ...
    async def resolve(self, ids: str | list[str]) -> UserRef | list[UserRef | None] | None:
        if isinstance(ids, list):
            checked = [assert_user_id(id) for id in ids]
            recs = await self._h.users.load_many(checked)
            return [user_ref(r, checked[i]) if r is not None else None for i, r in enumerate(recs)]
        id = assert_user_id(ids)
        rec = await self._h.users.load(id)
        return user_ref(rec, id) if rec is not None else None

    @overload
    async def get_dnd(self, ids: str) -> DndInfo: ...
    @overload
    async def get_dnd(self, ids: list[str]) -> dict[str, DndInfo]: ...
    async def get_dnd(self, ids: str | list[str]) -> DndInfo | dict[str, DndInfo]:
        if isinstance(ids, list):
            return await self._h.fetch_dnd([assert_user_id(id) for id in ids])
        id = assert_user_id(ids)
        found = await self._h.fetch_dnd([id])
        return found.get(id) or DndInfo(enabled=False, next_start_ts=0, next_end_ts=0)


@dataclass(kw_only=True, slots=True)
class HistoryThread:
    root_ts: str
    reply_count: int
    latest_reply_ts: str | None = None


@dataclass(kw_only=True, slots=True)
class HistoryMessage:
    ts: str
    sender: UserRef
    markdown: str
    files: list[FileRef]
    thread: HistoryThread | None


@dataclass(kw_only=True, slots=True)
class HistoryPage:
    messages: list[HistoryMessage]
    has_more: bool
    next_cursor: str | None = None


@dataclass(kw_only=True, slots=True)
class RawHistoryPage:
    messages: list[Obj] = field(default_factory=list)
    has_more: bool = False
    next_cursor: str | None = None


class ChannelsApi:
    def __init__(self, core: GabrielCore, h: Hydrator) -> None:
        self._core = core
        self._h = h

    async def _resolve_one(self, id: str) -> ChannelRef | None:
        rec = await self._h.load_channel(assert_channel_id(id))
        if rec is None:
            return None
        ref = channel_ref(rec, id)
        if rec.is_im and rec.im_user_id:
            try:
                await self._h.users.load(rec.im_user_id)
            except Exception:
                pass
            name = self._h.resolver.channel_name(id) if self._h.resolver.channel_name else None
            if name:
                ref.name = name
        return ref

    @overload
    async def resolve(self, ids: str) -> ChannelRef | None: ...
    @overload
    async def resolve(self, ids: list[str]) -> list[ChannelRef | None]: ...
    async def resolve(self, ids: str | list[str]) -> ChannelRef | list[ChannelRef | None] | None:
        if isinstance(ids, list):
            return list(await asyncio.gather(*(self._resolve_one(id) for id in ids)))
        return await self._resolve_one(ids)

    async def get_history(
        self,
        channel: str,
        *,
        limit: int = 50,
        oldest: str | None = None,
        latest: str | None = None,
        cursor: str | None = None,
        inclusive: bool = False,
    ) -> HistoryPage:
        """Channel history (main timeline + broadcast replies; plain thread replies are not included)."""
        res = await self.raw_history(
            channel, limit=limit, oldest=oldest, latest=latest, cursor=cursor, inclusive=inclusive
        )
        rendered = await _render_messages(self._h, res.messages)
        messages = [_to_history_message(r) for r in rendered]
        return HistoryPage(messages=messages, has_more=res.has_more, next_cursor=res.next_cursor)

    async def raw_history(
        self,
        channel: str,
        *,
        limit: int = 50,
        oldest: str | None = None,
        latest: str | None = None,
        cursor: str | None = None,
        inclusive: bool = False,
    ) -> RawHistoryPage:
        """Raw ``conversations.history`` page (used by gap recovery)."""
        channel = assert_channel_id(channel)
        if oldest is not None:
            assert_ts(oldest, "oldest")
        if latest is not None:
            assert_ts(latest, "latest")
        fields: Obj = {
            "channel": channel,
            "limit": min(200, max(1, limit)),
            "inclusive": inclusive,
            "include_pin_count": False,
            "no_user_profile": True,
            "include_free_team_extra_messages": True,
            "include_date_joined": False,
            "cached_latest_updates": "{}",
        }
        if oldest:
            fields["oldest"] = oldest
        if latest:
            fields["latest"] = latest
        if cursor:
            fields["cursor"] = cursor
        res = await self._core.call_web_api(
            "conversations.history", fields, RequestOpts(reason="history-api/fetchHistory")
        )
        return RawHistoryPage(
            messages=[m for m in res.get("messages") or [] if isinstance(m, dict)],
            has_more=res.get("has_more") is True,
            next_cursor=_s(_o(res.get("response_metadata")).get("next_cursor")),
        )

    async def iterate_history(
        self, channel: str, *, oldest: str | None = None, page_size: int = 100
    ) -> AsyncIterator[HistoryMessage]:
        """Iterates history newest → oldest. The upper bound is pinned on the first page."""
        cursor: str | None = None
        latest: str | None = None
        while True:
            page = await self.raw_history(channel, limit=page_size, oldest=oldest, latest=latest, cursor=cursor)
            if latest is None:
                first = page.messages[0] if page.messages else None
                first_ts = first.get("ts") if first is not None else None
                latest = first_ts if is_ts(first_ts) else None
                if latest:
                    cursor = page.next_cursor
            else:
                cursor = page.next_cursor
            for r in await _render_messages(self._h, page.messages):
                yield _to_history_message(r)
            if not page.has_more or not cursor:
                return


def _to_history_message(r: _Rendered) -> HistoryMessage:
    msg = r.msg
    reply_count = msg.get("reply_count")
    thread_ts = _s(msg.get("thread_ts"))
    return HistoryMessage(
        ts=_s(msg.get("ts")) or "",
        sender=r.sender,
        markdown=r.markdown,
        files=r.files,
        thread=HistoryThread(
            root_ts=thread_ts,
            reply_count=reply_count if isinstance(reply_count, int) else 0,
            latest_reply_ts=_s(msg.get("latest_reply")),
        )
        if thread_ts
        else None,
    )
