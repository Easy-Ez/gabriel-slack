"""Least-privilege sandbox view for LLM tool use (spec/behavior.md §11)."""

from __future__ import annotations

import inspect
import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, NoReturn, overload

from ..errors import GabrielError
from ..events.types import ChannelRef, DndInfo, UserRef
from ..util.aio import AbortSignal
from ..util.logger import Logger
from .context import current_trigger

if TYPE_CHECKING:
    from .apis import (
        ChannelsApi,
        FileInfo,
        HistoryMessage,
        HistoryPage,
        ReactionsApi,
        SearchApi,
        SearchMessageHit,
        SearchPage,
        SearchSort,
        ThreadContext,
        ThreadMessage,
        ThreadsApi,
        UsersApi,
    )
    from .chat import ChatApi, SendResult, UpdateResult
    from .files import FileLike, FilesApi, UploadContent
    from .origin import SelfRegistry
    from .presence import PresenceApi, PresenceStatus
    from .stream import StreamController, StreamResult

ScopedWriteAction = Literal[
    "chat.send",
    "chat.create_stream",
    "chat.update",
    "chat.delete",
    "reactions.add",
    "reactions.remove",
    "files.upload",
    "files.delete",
]

ApprovalHook = Callable[[str, dict[str, Any]], Awaitable[bool] | bool]


@dataclass(kw_only=True, slots=True)
class ReadPolicy:
    #: Readable channel allow-list (history, thread context, file downloads, search hits).
    channels: Sequence[str] | None = None
    allow_search: bool = True
    max_file_download_bytes: int | None = None


@dataclass(kw_only=True, slots=True)
class WritePolicy:
    #: Only reply / react in the channel + thread that triggered the running handler.
    target_scope: Literal["trigger_thread_only"] | None = None
    allowed_actions: Sequence[ScopedWriteAction] | None = None
    #: Appended to every written message (AI-generated content disclosure).
    disclosure_suffix: str = ""


@dataclass(kw_only=True, slots=True)
class ScopePolicy:
    read: ReadPolicy | None = None
    write: WritePolicy | None = None
    #: Human approval hook, consulted for every write; return False to refuse.
    approval: ApprovalHook | None = None


@dataclass(kw_only=True, slots=True)
class _Target:
    channel: str | None = None
    thread_ts: str | None = None
    ts: str | None = None


_FILE_ID = re.compile(r"^F[A-Z0-9]+$")


class _SuffixedStream:
    """Stream wrapper that appends the disclosure suffix on ``finish()``."""

    def __init__(self, stream: StreamController, suffix: str) -> None:
        self._stream = stream
        self._suffix = suffix
        self._finished = False

    @property
    def ts(self) -> str:
        return self._stream.ts

    @property
    def signal(self) -> AbortSignal:
        return self._stream.signal

    def append(self, chunk: str) -> None:
        self._stream.append(chunk)

    async def abort(self, reason: str = "aborted") -> None:
        await self._stream.abort(reason)

    async def finish(self) -> StreamResult:
        if not self._finished:
            self._finished = True
            self._stream.append(self._suffix)
        return await self._stream.finish()

    async def __aenter__(self) -> _SuffixedStream:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        if self._stream.signal.aborted or self._finished:
            return
        if exc is None:
            await self.finish()
        else:
            await self.abort(type(exc).__name__)


class _ScopedChat:
    def __init__(self, scope: ScopedClient, chat: ChatApi) -> None:
        self._scope = scope
        self._chat = chat

    async def send(
        self,
        channel: str,
        markdown: str | None = None,
        *,
        blocks: list[Any] | None = None,
        thread_ts: str | None = None,
        broadcast: bool = False,
        priority: Literal["interactive", "normal"] = "normal",
        preserve_unread_cursor: bool = False,
        auto_chunk: bool = True,
    ) -> SendResult:
        params = {"channel": channel, "markdown": markdown, "blocks": blocks, "thread_ts": thread_ts}
        await self._scope._check_write("chat.send", _Target(channel=channel, thread_ts=thread_ts), params)
        return await self._chat.send(
            channel,
            self._scope._with_suffix(markdown),
            blocks=blocks,
            thread_ts=thread_ts,
            broadcast=broadcast,
            priority=priority,
            preserve_unread_cursor=preserve_unread_cursor,
            auto_chunk=auto_chunk,
        )

    async def update(
        self, channel: str, ts: str, markdown: str | None = None, *, blocks: list[Any] | None = None
    ) -> UpdateResult:
        params = {"channel": channel, "ts": ts, "markdown": markdown, "blocks": blocks}
        await self._scope._check_write("chat.update", _Target(channel=channel, ts=ts), params)
        return await self._chat.update(channel, ts, self._scope._with_suffix(markdown), blocks=blocks)

    async def delete(self, channel: str, ts: str) -> None:
        await self._scope._check_write("chat.delete", _Target(channel=channel, ts=ts), {"channel": channel, "ts": ts})
        await self._chat.delete(channel, ts)

    async def create_stream(
        self,
        channel: str,
        *,
        thread_ts: str | None = None,
        initial_markdown: str | None = None,
        preserve_unread_cursor: bool = False,
    ) -> StreamController:
        params = {"channel": channel, "thread_ts": thread_ts, "initial_markdown": initial_markdown}
        await self._scope._check_write("chat.create_stream", _Target(channel=channel, thread_ts=thread_ts), params)
        stream = await self._chat.create_stream(
            channel,
            thread_ts=thread_ts,
            initial_markdown=initial_markdown,
            preserve_unread_cursor=preserve_unread_cursor,
        )
        suffix = self._scope._suffix
        return _SuffixedStream(stream, suffix) if suffix else stream

    async def send_typing(self, channel: str, *, thread_ts: str | None = None) -> None:
        self._scope._check_read(channel)
        await self._chat.send_typing(channel, thread_ts=thread_ts)


class _ScopedThreads:
    def __init__(self, scope: ScopedClient, threads: ThreadsApi) -> None:
        self._scope = scope
        self._threads = threads

    async def get_context(self, channel: str, thread_ts: str, *, limit: int = 50) -> ThreadContext:
        self._scope._check_read(channel)
        return await self._threads.get_context(channel, thread_ts, limit=limit)

    def iterate_replies(self, channel: str, thread_ts: str, *, page_size: int = 100) -> AsyncIterator[ThreadMessage]:
        self._scope._check_read(channel)
        return self._threads.iterate_replies(channel, thread_ts, page_size=page_size)


class _ScopedFiles:
    def __init__(self, scope: ScopedClient, files: FilesApi) -> None:
        self._scope = scope
        self._files = files

    async def upload(
        self,
        filename: str,
        content: UploadContent,
        *,
        size: int | None = None,
        title: str | None = None,
        mime_type: str | None = None,
        channel: str | None = None,
        thread_ts: str | None = None,
        intro_markdown: str | None = None,
    ) -> FileInfo:
        await self._scope._check_write(
            "files.upload",
            _Target(channel=channel, thread_ts=thread_ts),
            {"filename": filename, "channel": channel},
        )
        return await self._files.upload(
            filename,
            content,
            size=size,
            title=title,
            mime_type=mime_type,
            channel=channel,
            thread_ts=thread_ts,
            intro_markdown=self._scope._with_suffix(intro_markdown),
        )

    async def get_info(self, file_id: str) -> FileInfo:
        info = await self._files.get_info(file_id)
        if not self._scope._file_allowed(info):
            raise self._scope._deny("files.get_info", "file is not shared in an allowed channel")
        return info

    async def delete(self, file_id: str) -> None:
        await self._scope._check_write("files.delete", _Target(), {"file_id": file_id})
        await self._files.delete(file_id)

    async def download_stream(
        self, file: FileLike, *, max_bytes: int | None = None, signal: AbortSignal | None = None
    ) -> AsyncIterator[bytes]:
        scope = self._scope
        read = scope._policy.read
        limit = read.max_file_download_bytes if read is not None else None
        if isinstance(file, str):
            id = file if _FILE_ID.match(file) else None
        else:
            id = getattr(file, "id", None) or None
        if read is not None and read.channels is not None:
            if not id:
                raise scope._deny("files.download", "file must be identified by ID when read.channels is restricted")
            info = await self._files.get_info(id)
            if not scope._file_allowed(info):
                raise scope._deny("files.download", "file is not shared in an allowed channel")
            if limit is not None and info.size is not None and info.size > limit:
                raise scope._deny("files.download", f"file exceeds max_file_download_bytes ({limit})")
        file_size = getattr(file, "size", None) if not isinstance(file, str) else None
        if limit is not None and isinstance(file_size, int) and file_size > limit:
            raise scope._deny("files.download", f"file exceeds max_file_download_bytes ({limit})")
        effective = limit if max_bytes is None else max_bytes if limit is None else min(limit, max_bytes)
        scope_binding = limit is not None and (max_bytes is None or limit <= max_bytes)

        def remap(err: GabrielError) -> GabrielError:
            if scope_binding and err.code == "FILE_TOO_LARGE":
                return scope._deny("files.download", f"file exceeds max_file_download_bytes ({limit})")
            return err

        try:
            stream = await self._files.download_stream(file, max_bytes=effective, signal=signal)
        except GabrielError as err:
            raise remap(err) from err
        if not scope_binding:
            return stream

        # Policy limits win over resource limits: re-map mid-stream FILE_TOO_LARGE.
        async def guarded() -> AsyncIterator[bytes]:
            try:
                async for chunk in stream:
                    yield chunk
            except GabrielError as err:
                raise remap(err) from err

        return guarded()

    async def download_bytes(
        self, file: FileLike, *, max_bytes: int | None = None, signal: AbortSignal | None = None
    ) -> bytes:
        stream = await self.download_stream(file, max_bytes=max_bytes, signal=signal)
        return b"".join([chunk async for chunk in stream])


class _ScopedSearch:
    def __init__(self, scope: ScopedClient, search: SearchApi) -> None:
        self._scope = scope
        self._search = search

    async def messages(
        self, query: str, *, count: int = 20, page: int = 1, sort: SearchSort = "timestamp"
    ) -> SearchPage[SearchMessageHit]:
        self._scope._check_search()
        res = await self._search.messages(query, count=count, page=page, sort=sort)
        res.items = [h for h in res.items if self._scope._channel_allowed(h.channel.id)]
        return res

    async def files(
        self, query: str, *, count: int = 20, page: int = 1, sort: SearchSort = "timestamp"
    ) -> SearchPage[FileInfo]:
        self._scope._check_search()
        res = await self._search.files(query, count=count, page=page, sort=sort)
        res.items = [f for f in res.items if self._scope._file_allowed(f)]
        return res

    async def channels(
        self, query: str, *, count: int = 20, page: int = 1, sort: SearchSort = "timestamp"
    ) -> SearchPage[ChannelRef]:
        self._scope._check_search()
        res = await self._search.channels(query, count=count, page=page, sort=sort)
        res.items = [c for c in res.items if self._scope._channel_allowed(c.id)]
        return res

    async def people(
        self, query: str, *, count: int = 20, page: int = 1, sort: SearchSort = "timestamp"
    ) -> SearchPage[UserRef]:
        self._scope._check_search()
        return await self._search.people(query, count=count, page=page, sort=sort)

    def iterate_messages(
        self, query: str, *, count: int = 20, sort: SearchSort = "timestamp", max_pages: int = 50
    ) -> AsyncIterator[SearchMessageHit]:
        self._scope._check_search()

        async def gen() -> AsyncIterator[SearchMessageHit]:
            async for hit in self._search.iterate_messages(query, count=count, sort=sort, max_pages=max_pages):
                if self._scope._channel_allowed(hit.channel.id):
                    yield hit

        return gen()

    def iterate_files(
        self, query: str, *, count: int = 20, sort: SearchSort = "timestamp", max_pages: int = 50
    ) -> AsyncIterator[FileInfo]:
        self._scope._check_search()

        async def gen() -> AsyncIterator[FileInfo]:
            async for f in self._search.iterate_files(query, count=count, sort=sort, max_pages=max_pages):
                if self._scope._file_allowed(f):
                    yield f

        return gen()


class _ScopedReactions:
    def __init__(self, scope: ScopedClient, reactions: ReactionsApi) -> None:
        self._scope = scope
        self._reactions = reactions

    async def add(self, channel: str, ts: str, emoji: str) -> None:
        params = {"channel": channel, "ts": ts, "emoji": emoji}
        await self._scope._check_write("reactions.add", _Target(channel=channel, ts=ts), params)
        await self._reactions.add(channel, ts, emoji)

    async def remove(self, channel: str, ts: str, emoji: str) -> None:
        params = {"channel": channel, "ts": ts, "emoji": emoji}
        await self._scope._check_write("reactions.remove", _Target(channel=channel, ts=ts), params)
        await self._reactions.remove(channel, ts, emoji)


class _ScopedUsers:
    def __init__(self, users: UsersApi) -> None:
        self._users = users

    @overload
    async def resolve(self, ids: str) -> UserRef | None: ...
    @overload
    async def resolve(self, ids: list[str]) -> list[UserRef | None]: ...
    async def resolve(self, ids: str | list[str]) -> UserRef | list[UserRef | None] | None:
        return await self._users.resolve(ids)

    @overload
    async def get_dnd(self, ids: str) -> DndInfo: ...
    @overload
    async def get_dnd(self, ids: list[str]) -> dict[str, DndInfo]: ...
    async def get_dnd(self, ids: str | list[str]) -> DndInfo | dict[str, DndInfo]:
        return await self._users.get_dnd(ids)


class _ScopedChannels:
    def __init__(self, scope: ScopedClient, channels: ChannelsApi) -> None:
        self._scope = scope
        self._channels = channels

    @overload
    async def resolve(self, ids: str) -> ChannelRef | None: ...
    @overload
    async def resolve(self, ids: list[str]) -> list[ChannelRef | None]: ...
    async def resolve(self, ids: str | list[str]) -> ChannelRef | list[ChannelRef | None] | None:
        return await self._channels.resolve(ids)

    async def get_history(
        self,
        channel: str,
        *,
        limit: int = 50,
        oldest: str | None = None,
        latest: str | None = None,
        cursor: str | None = None,
    ) -> HistoryPage:
        self._scope._check_read(channel)
        return await self._channels.get_history(channel, limit=limit, oldest=oldest, latest=latest, cursor=cursor)

    def iterate_history(
        self, channel: str, *, oldest: str | None = None, page_size: int = 100
    ) -> AsyncIterator[HistoryMessage]:
        self._scope._check_read(channel)
        return self._channels.iterate_history(channel, oldest=oldest, page_size=page_size)


class _ScopedPresence:
    def __init__(self, presence: PresenceApi) -> None:
        self._presence = presence

    async def get_status(self, user_id: str) -> PresenceStatus:
        return await self._presence.get_status(user_id)


class ScopedClient:
    """Least-privilege view over a client.

    Same API surface; every out-of-policy call raises SCOPE_POLICY_VIOLATION and is audited.
    The escape hatch (``core``) is not available on scoped views.
    """

    def __init__(
        self,
        policy: ScopePolicy,
        *,
        chat: ChatApi,
        threads: ThreadsApi,
        files: FilesApi,
        search: SearchApi,
        reactions: ReactionsApi,
        users: UsersApi,
        channels: ChannelsApi,
        presence: PresenceApi,
        registry: SelfRegistry,
        logger: Logger,
    ) -> None:
        self._policy = policy
        self._registry = registry
        self._logger = logger
        self._suffix = policy.write.disclosure_suffix if policy.write is not None else ""
        self.chat = _ScopedChat(self, chat)
        self.threads = _ScopedThreads(self, threads)
        self.files = _ScopedFiles(self, files)
        self.search = _ScopedSearch(self, search)
        self.reactions = _ScopedReactions(self, reactions)
        self.users = _ScopedUsers(users)
        self.channels = _ScopedChannels(self, channels)
        self.presence = _ScopedPresence(presence)

    @property
    def core(self) -> NoReturn:
        """The escape hatch bypasses every guard and is never exposed on a scoped view."""
        raise self._deny("core", "client.core is not available on a scoped client")

    def _with_suffix(self, md: str | None) -> str | None:
        return f"{md}{self._suffix}" if md is not None and self._suffix else md

    def _channel_allowed(self, id: str) -> bool:
        read = self._policy.read
        return read is None or read.channels is None or id in read.channels

    def _file_allowed(self, info: FileInfo) -> bool:
        read = self._policy.read
        if read is None or read.channels is None:
            return True
        return any(c in read.channels for c in info.shared_in or [])

    def _deny(self, action: str, reason: str) -> GabrielError:
        trigger = current_trigger()
        self._logger.warn(
            {"action": action, "reason": reason, "trigger_event_id": trigger.event_id if trigger else None},
            "scope policy violation",
        )
        return GabrielError("SCOPE_POLICY_VIOLATION", f"{action}: {reason}")

    def _check_read(self, channel: str) -> None:
        read = self._policy.read
        if read is not None and read.channels is not None and channel not in read.channels:
            raise self._deny("read", f"channel {channel} is not in read.channels")

    def _check_search(self) -> None:
        read = self._policy.read
        if read is not None and read.allow_search is False:
            raise self._deny("search", "search is disabled by policy")

    async def _check_write(self, action: ScopedWriteAction, target: _Target, params: dict[str, Any]) -> None:
        w = self._policy.write
        if w is not None and w.allowed_actions is not None and action not in w.allowed_actions:
            raise self._deny(action, "action is not in write.allowed_actions")
        if w is not None and w.target_scope == "trigger_thread_only":
            trigger = current_trigger()
            if trigger is None or not trigger.channel:
                raise self._deny(
                    action, "trigger_thread_only writes must happen inside an event handler with a channel"
                )
            if target.channel != trigger.channel:
                raise self._deny(action, "target channel is not the trigger channel")
            if action in ("chat.send", "chat.create_stream", "files.upload"):
                if not trigger.thread_root_ts or target.thread_ts != trigger.thread_root_ts:
                    raise self._deny(action, "target thread is not the trigger thread")
            elif action in ("reactions.add", "reactions.remove"):
                if target.ts != trigger.ts and target.ts != trigger.thread_root_ts:
                    raise self._deny(action, "reactions are limited to the trigger message")
            elif action in ("chat.update", "chat.delete"):
                if not target.ts or not self._registry.is_self_sent(target.ts):
                    raise self._deny(action, "only messages this client sent can be changed")
            elif action == "files.delete":
                raise self._deny(action, "files.delete has no thread target under trigger_thread_only")
        if self._policy.approval is not None:
            ok = False
            try:
                result = self._policy.approval(action, params)
                if inspect.isawaitable(result):
                    result = await result
                ok = result is True
            except Exception as err:
                self._logger.warn({"action": action, "err": str(err)}, "approval hook threw; treating as refusal")
            if not ok:
                raise self._deny(action, "refused by approval hook")
