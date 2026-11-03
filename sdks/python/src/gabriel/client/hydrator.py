"""Entity resolution cache shared by event hydration and the users/channels APIs."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..core.types import RequestOpts
from ..events.types import ChannelRef, DndInfo, ReactionEvent, UserRef, UserStatus
from ..markdown.blocks_to_markdown import EntityResolver
from ..util.aio import chunk
from ..util.logger import Logger
from ..util.lru import LruCache
from .loader import BatchLoader

if TYPE_CHECKING:
    from ..core.core import GabrielCore
    from ..events.normalize import Draft

Obj = dict[str, Any]


def _s(v: Any) -> str | None:
    return v if isinstance(v, str) and len(v) > 0 else None


def _o(v: Any) -> Obj:
    return v if isinstance(v, dict) else {}


def _num(v: Any) -> int | float | None:
    return v if isinstance(v, int | float) and not isinstance(v, bool) else None


@dataclass(kw_only=True, slots=True)
class UserRecord(UserRef):
    """Cached user record (superset of the public :class:`UserRef`)."""

    status: UserStatus
    real_name: str | None = None
    team_id: str | None = None
    deleted: bool | None = None


@dataclass(kw_only=True, slots=True)
class ChannelRecord(ChannelRef):
    #: Peer user of a 1:1 DM.
    im_user_id: str | None = None


@dataclass(kw_only=True, slots=True)
class UsergroupRecord:
    id: str
    handle: str
    name: str | None = None
    users: list[str] | None = None


def to_user_record(raw: Obj, self_team_id: str) -> UserRecord:
    profile = _o(raw.get("profile"))
    name = _s(raw.get("name"))
    real_name = _s(raw.get("real_name")) or _s(profile.get("real_name"))
    display_name = _s(profile.get("display_name")) or real_name or name
    team_id = _s(raw.get("team_id")) or _s(profile.get("team"))
    expiration = _num(profile.get("status_expiration"))
    rec = UserRecord(
        id=str(raw.get("id")),
        is_bot=raw.get("is_bot") is True,
        status=UserStatus(
            emoji=_s(profile.get("status_emoji")) or "",
            text=_s(profile.get("status_text")) or "",
            expiration=expiration if expiration is not None else 0,
        ),
    )
    if name:
        rec.name = name
        rec.handle = name
    if display_name:
        rec.display_name = display_name
    if real_name:
        rec.real_name = real_name
    if team_id:
        rec.team_id = team_id
    # Enterprise-grid colleagues share the enterprise; stranger / foreign-team users are external.
    rec.is_external = raw.get("is_stranger") is True or (
        team_id is not None and team_id != self_team_id and not raw.get("enterprise_user")
    )
    if raw.get("deleted") is True:
        rec.deleted = True
    return rec


def to_channel_record(raw: Obj) -> ChannelRecord:
    rec = ChannelRecord(id=str(raw.get("id")))
    name = _s(raw.get("name"))
    if name:
        rec.name = name
    if isinstance(raw.get("is_im"), bool):
        rec.is_im = raw["is_im"]
    if isinstance(raw.get("is_mpim"), bool):
        rec.is_mpim = raw["is_mpim"]
    if isinstance(raw.get("is_private"), bool):
        rec.is_private = raw["is_private"]
    elif raw.get("is_group") is True or raw.get("is_im") is True or raw.get("is_mpim") is True:
        rec.is_private = True
    elif raw.get("is_channel") is True:
        rec.is_private = False
    if _s(raw.get("user")):
        rec.im_user_id = raw["user"]
    return rec


def to_dnd_info(raw: Obj) -> DndInfo:
    start = _num(raw.get("next_dnd_start_ts"))
    end = _num(raw.get("next_dnd_end_ts"))
    return DndInfo(
        enabled=raw.get("dnd_enabled") is True,
        next_start_ts=start if start is not None else 0,
        next_end_ts=end if end is not None else 0,
    )


def user_ref(rec: UserRef | None, id: str) -> UserRef:
    """Public :class:`UserRef` view of a record (drops record-only fields)."""
    if rec is None:
        return UserRef(id=id)
    return UserRef(
        id=id,
        name=rec.name or None,
        display_name=rec.display_name or None,
        handle=rec.handle or None,
        is_bot=rec.is_bot,
        is_external=rec.is_external,
    )


def channel_ref(rec: ChannelRef | None, id: str) -> ChannelRef:
    """Public :class:`ChannelRef` view of a record."""
    if rec is None:
        return ChannelRef(id=id)
    return ChannelRef(id=id, name=rec.name or None, is_private=rec.is_private, is_im=rec.is_im, is_mpim=rec.is_mpim)


@dataclass(slots=True)
class HydrationResult:
    complete: bool
    _all: asyncio.Future[Any] | None = field(default=None, repr=False)
    _draft: Draft | None = field(default=None, repr=False)
    _hydrator: Hydrator | None = field(default=None, repr=False)

    async def remaining(self) -> None:
        """Completes all lookups and re-renders the draft."""
        if self._all is not None:
            await asyncio.shield(self._all)
        if self._draft is not None and self._hydrator is not None:
            self._draft.render(self._hydrator.resolver)


class Hydrator:
    def __init__(self, core: GabrielCore, logger: Logger) -> None:
        self._core = core
        self._logger = logger
        self._usergroups: dict[str, UsergroupRecord] = {}
        self.my_subteams: set[str] = set()
        #: message ts → thread root ts ("" = not in a thread)
        self._thread_roots: LruCache[str, str] = LruCache(20_000, 60 * 60.0)

        async def load_users(ids: list[str]) -> dict[str, UserRecord]:
            res = await core.call_edge_api(
                "users/info",
                {
                    "check_interaction": True,
                    "include_profile_only_users": True,
                    "updated_ids": {id: 0 for id in ids},
                },
            )
            team_id = core.session.team_id
            out: dict[str, UserRecord] = {}
            for raw in res.get("results") or []:
                if isinstance(raw, dict) and _s(raw.get("id")):
                    out[str(raw["id"])] = to_user_record(raw, team_id)
            return out

        async def load_channels(ids: list[str]) -> dict[str, ChannelRecord]:
            res = await core.call_edge_api(
                "channels/info", {"check_membership": True, "updated_ids": {id: 0 for id in ids}}
            )
            out: dict[str, ChannelRecord] = {}
            for raw in res.get("results") or []:
                if isinstance(raw, dict) and _s(raw.get("id")):
                    out[str(raw["id"])] = to_channel_record(raw)
            return out

        # 1:1 DMs are not served by EdgeAPI; resolve them one by one via conversations.info.
        async def load_ims(ids: list[str]) -> dict[str, ChannelRecord]:
            from ..errors import GabrielError

            out: dict[str, ChannelRecord] = {}
            for id in ids:
                try:
                    res = await core.call_web_api(
                        "conversations.info", {"channel": id}, RequestOpts(reason="resolve-dm-info")
                    )
                    if isinstance(res.get("channel"), dict):
                        out[id] = to_channel_record(res["channel"])
                except GabrielError as err:
                    if err.code != "CHANNEL_NOT_FOUND":
                        raise
            return out

        self.users: BatchLoader[UserRecord] = BatchLoader(load_users)
        self.channels: BatchLoader[ChannelRecord] = BatchLoader(load_channels)
        self._ims: BatchLoader[ChannelRecord] = BatchLoader(load_ims, max_batch=10, concurrency=1)
        #: Synchronous name lookup used for Markdown rendering.
        self.resolver = EntityResolver(
            user_name=self._user_name, channel_name=self._channel_name, usergroup_handle=self._usergroup_handle
        )

    def _user_name(self, id: str) -> str | None:
        rec = self.users.peek(id)
        return rec.display_name if rec is not None else None

    def _channel_name(self, id: str) -> str | None:
        ch = self.peek_channel(id)
        if ch is None:
            return None
        if ch.is_im and ch.im_user_id:
            peer = self.users.peek(ch.im_user_id)
            return f"@{peer.display_name}" if peer is not None and peer.display_name else None
        return ch.name

    def _usergroup_handle(self, id: str) -> str | None:
        g = self._usergroups.get(id)
        return g.handle if g is not None and g.handle else None

    def _channel_loader(self, id: str) -> BatchLoader[ChannelRecord]:
        return self._ims if id.startswith("D") else self.channels

    def peek_channel(self, id: str) -> ChannelRecord | None:
        return self._channel_loader(id).peek(id)

    def is_channel_known(self, id: str) -> bool:
        return self._channel_loader(id).is_known(id)

    def load_channel(self, id: str, *, fresh: bool = False) -> asyncio.Future[ChannelRecord | None]:
        return self._channel_loader(id).load(id, fresh=fresh)

    def prime_channel(self, rec: ChannelRecord) -> None:
        self._channel_loader(rec.id).prime(rec.id, rec)

    def invalidate_channel(self, id: str) -> None:
        self._channel_loader(id).invalidate(id)

    def usergroup(self, id: str) -> UsergroupRecord | None:
        return self._usergroups.get(id)

    def upsert_usergroup(self, rec: UsergroupRecord) -> None:
        prev = self._usergroups.get(rec.id)
        if prev is None:
            self._usergroups[rec.id] = rec
            return
        self._usergroups[rec.id] = UsergroupRecord(
            id=rec.id,
            handle=rec.handle,
            name=rec.name if rec.name is not None else prev.name,
            users=rec.users if rec.users is not None else prev.users,
        )

    def record_thread_root(self, ts: str, root_ts: str | None) -> None:
        self._thread_roots.set(ts, root_ts or "")

    async def preload_usergroups(self, self_user_id: str) -> None:
        """Loads usergroup handles and my memberships (best effort)."""
        try:
            res = await self._core.call_web_api(
                "usergroups.list", {"include_users": True}, RequestOpts(reason="gabriel-usergroups")
            )
            for g in res.get("usergroups") or []:
                if not isinstance(g, dict):
                    continue
                id = _s(g.get("id"))
                if not id:
                    continue
                users = [u for u in g["users"] if isinstance(u, str)] if isinstance(g.get("users"), list) else None
                self.upsert_usergroup(
                    UsergroupRecord(id=id, handle=_s(g.get("handle")) or "", name=_s(g.get("name")), users=users)
                )
                if users is not None and self_user_id in users:
                    self.my_subteams.add(id)
        except Exception as err:
            self._logger.warn({"err": str(err)}, "usergroup preload failed; usergroup mentions may be incomplete")

    async def thread_root_of(self, channel: str, ts: str) -> str | None:
        """Thread root of a message ts within a channel (None = not threaded)."""
        cached = self._thread_roots.get(ts)
        if cached is not None:
            return cached or None
        res = await self._core.call_web_api(
            "conversations.replies",
            {"channel": channel, "ts": ts, "limit": 1, "inclusive": True},
            RequestOpts(reason="history-api/fetchReplies"),
        )
        messages = res.get("messages") or []
        first = messages[0] if messages and isinstance(messages[0], dict) else None
        root = _s(first.get("thread_ts")) if first is not None else None
        self._thread_roots.set(ts, root or "")
        return root

    async def hydrate(self, draft: Draft, budget: float) -> HydrationResult:
        """Starts entity lookups for a draft and renders it within ``budget`` seconds."""
        tasks: list[Awaitable[Any]] = []
        for id in draft.needs.users:
            if not self.users.is_known(id):
                tasks.append(self.users.load(id))
        for id in draft.needs.channels:
            if not self.is_channel_known(id):
                tasks.append(self._load_channel_and_peer(id))
        target = draft.reaction_target
        if target is not None:
            tasks.append(self._resolve_reaction_root(draft, target.channel, target.ts))

        def finish(complete: bool, all_fut: asyncio.Future[Any] | None) -> HydrationResult:
            try:
                draft.render(self.resolver)
            except Exception as err:
                self._logger.error({"err": str(err)}, "render failed")
            return HydrationResult(complete, all_fut, draft, self)

        if not tasks:
            return finish(True, None)
        all_fut = asyncio.ensure_future(self._settle_all(tasks))
        done, _ = await asyncio.wait({all_fut}, timeout=budget)
        return finish(all_fut in done, all_fut)

    async def _settle_all(self, tasks: list[Awaitable[Any]]) -> None:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, BaseException):
                self._logger.debug({"err": str(r)}, "hydration lookup failed")

    async def _load_channel_and_peer(self, id: str) -> None:
        ch = await self.load_channel(id)
        if ch is not None and ch.im_user_id and not self.users.is_known(ch.im_user_id):
            await self.users.load(ch.im_user_id)

    async def _resolve_reaction_root(self, draft: Draft, channel: str, ts: str) -> None:
        root = await self.thread_root_of(channel, ts)
        if root and isinstance(draft.event, ReactionEvent):
            draft.event.target.thread_root_ts = root

    async def fetch_dnd(self, ids: list[str]) -> dict[str, DndInfo]:
        """DND lookup in batches of 30 (``dnd.teamInfo``)."""
        out: dict[str, DndInfo] = {}
        for batch in chunk(list(dict.fromkeys(ids)), 30):
            res = await self._core.call_web_api(
                "dnd.teamInfo", {"users": ",".join(batch)}, RequestOpts(reason="fetchAndUpsertDndForUsers")
            )
            for uid, raw in (res.get("users") or {}).items():
                if isinstance(raw, dict):
                    out[uid] = to_dnd_info(raw)
        return out
