"""Presence, profile-status and DND subscriptions (spec/behavior.md §7)."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal

from ..core.types import RawWsFrame
from ..errors import GabrielError
from ..events.types import (
    DndInfo,
    PresenceChangedEvent,
    UserDndChangedEvent,
    UserProfileChangedEvent,
    UserProfileField,
    UserProfilePrevious,
    UserProfileRef,
    UserRef,
    UserStatus,
    UserStatusChangedEvent,
)
from ..util.aio import chunk, spawn
from ..util.logger import Logger
from ..util.validate import assert_user_id
from .hydrator import Hydrator, UserRecord, to_user_record

if TYPE_CHECKING:
    from ..core.core import GabrielCore

#: Serialized ``presence_sub`` frames above this many bytes are silently dropped by the gateway (16 KiB).
PRESENCE_FRAME_MAX_BYTES = 16_000
PROFILE_POOL_CAP = 900
_PROFILE_FRAME_BATCH = 100
_PRESENCE_DEBOUNCE = 0.100
_SNAPSHOT_TIMEOUT = 15.0
_GATEWAY_ID_CAP = 12
_GATEWAY_RATE_LIMIT = 15

PresenceValue = Literal["active", "away", "unknown"]


class SubscriptionHandle:
    """Ref-counted subscription. Release with :meth:`unsubscribe` or ``async with``."""

    def __init__(self, user_ids: list[str], release: Callable[[], None]) -> None:
        self._user_ids = tuple(user_ids)
        self._release = release
        self._released = False

    @property
    def user_ids(self) -> tuple[str, ...]:
        return self._user_ids

    async def unsubscribe(self) -> None:
        if self._released:
            return
        self._released = True
        self._release()

    async def __aenter__(self) -> SubscriptionHandle:
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
    ) -> None:
        await self.unsubscribe()


@dataclass(kw_only=True, slots=True)
class PresenceStatus:
    presence: PresenceValue
    status: UserStatus | None = None
    dnd: DndInfo | None = None


@dataclass(slots=True)
class _PresenceEntry:
    presence: PresenceValue
    epoch: int
    updated_at: float


Emit = Callable[
    [PresenceChangedEvent | UserStatusChangedEvent | UserProfileChangedEvent | UserDndChangedEvent, str], None
]

_PROFILE_FIELDS: tuple[tuple[UserProfileField, str], ...] = (
    ("displayName", "display_name"),
    ("realName", "real_name"),
    ("handle", "handle"),
)


def profile_changes(before: UserRecord, after: UserRecord) -> list[UserProfileField]:
    """Name fields that differ between two records; a field missing from ``after`` never counts."""
    return [
        name
        for name, attr in _PROFILE_FIELDS
        if getattr(after, attr) is not None and getattr(after, attr) != getattr(before, attr)
    ]


def presence_frame_bytes(ids: Iterable[str]) -> int:
    """Byte length of the presence_sub frame for a set of ids (id field included)."""
    frame = {"type": "presence_sub", "ids": list(ids), "id": 2_147_483_647}
    return len(json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _brief(rec: UserRecord | None, user_id: str) -> UserRef:
    return UserRef(
        id=user_id,
        display_name=rec.display_name if rec is not None and rec.display_name else None,
        handle=rec.handle if rec is not None and rec.handle else None,
    )


def _gateway_error_code(err: BaseException) -> int | None:
    """Gateway error code of a failed ``subscription_update`` command, if the reply frame carried one."""
    frame = getattr(err, "frame", None)
    if not isinstance(frame, dict):
        cause = err.__cause__
        frame = getattr(cause, "frame", None) if cause is not None else None
    if not isinstance(frame, dict):
        return None
    error = frame.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("gatewayErrorCode", error.get("gateway_error_code"))
    return code if isinstance(code, int) else None


class PresenceApi:
    """Two independently-capped subscription pools with single-writer reconcilers.

    - presence pool: full-overwrite ``presence_sub``, no ACK, debounced, <= 16,000 bytes/frame;
    - profile/DND pool: incremental ``subscription_update``, 100 ids/frame, ACKed, <= 900 ids.
      ``user_change`` subscriptions come from :meth:`watch_profiles` and
      ``watch(track_profile_and_dnd=True)``; DND subscriptions only from the latter.

    Desired sets are ref-counted; every ``hello`` starts a new epoch and replays both pools.
    """

    def __init__(self, core: GabrielCore, hydrator: Hydrator, emit: Emit, logger: Logger) -> None:
        self._core = core
        self._hydrator = hydrator
        self._emit = emit
        self._logger = logger
        self._presence_refs: dict[str, int] = {}
        self._profile_refs: dict[str, int] = {}
        self._dnd_refs: dict[str, int] = {}
        self._presence_applied: set[str] = set()
        self._profile_applied: set[str] = set()
        self._dnd_applied: set[str] = set()
        #: Last known record of each profile-subscribed user; outlives the hydrator's cache TTL.
        self._baseline: dict[str, UserRecord] = {}
        self._cache: dict[str, _PresenceEntry] = {}
        self._last_dnd: dict[str, DndInfo] = {}
        self._epoch = 0
        self._presence_timer: asyncio.TimerHandle | None = None
        self._profile_running: asyncio.Task[None] | None = None
        self._profile_dirty = False
        self._profile_full = False
        self._seq = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        self._timers: set[asyncio.TimerHandle] = set()

    async def watch(self, user_ids: list[str], *, track_profile_and_dnd: bool = False) -> SubscriptionHandle:
        """Watch users' presence (and optionally profile status + DND). Ref-counted."""
        if not isinstance(user_ids, list | tuple) or len(user_ids) == 0:
            raise GabrielError("INVALID_ARGUMENT", "presence.watch: user_ids must be a non-empty list")
        ids = list(dict.fromkeys(assert_user_id(u) for u in user_ids))
        track = track_profile_and_dnd

        next_presence = list(dict.fromkeys([*self._presence_refs, *ids]))
        size = presence_frame_bytes(next_presence)
        if size > PRESENCE_FRAME_MAX_BYTES:
            raise GabrielError(
                "SUBSCRIPTION_CAP_EXCEEDED",
                f"presence subscription would serialize to {size} bytes (> {PRESENCE_FRAME_MAX_BYTES}); "
                "about 1,150 standard user IDs fit",
            )
        if track:
            self._assert_profile_cap(ids)

        newly_presence: list[str] = []
        for id in ids:
            n = self._presence_refs.get(id, 0)
            self._presence_refs[id] = n + 1
            if n == 0:
                newly_presence.append(id)
                # A stale entry from an unwatched period must not suppress the initial snapshot.
                entry = self._cache.get(id)
                if entry is not None and entry.epoch != self._epoch:
                    del self._cache[id]
        if track:
            for id in ids:
                self._dnd_refs[id] = self._dnd_refs.get(id, 0) + 1

        # Users already snapshotted on this connection: the gateway will not re-push, so synthesize.
        def synthesize() -> None:
            for id in ids:
                entry = self._cache.get(id)
                if entry is not None and entry.epoch == self._epoch and entry.presence != "unknown":
                    self._emit_presence(id, entry.presence, "unknown", True)

        asyncio.get_running_loop().call_soon(synthesize)
        if newly_presence:
            self._schedule_presence()
        if track:
            self._retain_profiles(ids)

        def release() -> None:
            presence_changed = False
            for id in ids:
                n = self._presence_refs.get(id, 1) - 1
                if n <= 0:
                    self._presence_refs.pop(id, None)
                    presence_changed = True
                else:
                    self._presence_refs[id] = n
                if track:
                    d = self._dnd_refs.get(id, 1) - 1
                    if d <= 0:
                        self._dnd_refs.pop(id, None)
                    else:
                        self._dnd_refs[id] = d
            if track:
                self._release_profiles(ids)
            if presence_changed:
                self._schedule_presence()

        return SubscriptionHandle(ids, release)

    async def watch_profiles(self, user_ids: list[str]) -> SubscriptionHandle:
        """Subscribe to profile changes (``user_change``) of up to 900 users, without presence or DND.

        Slack pushes ``user_change`` / ``user_invalidated`` only for subscribed users, so this is how
        an application gets ``user.profile_changed`` (and ``user.status_changed``) for the users it
        shows. Ref-counted.
        """
        if not isinstance(user_ids, list | tuple) or len(user_ids) == 0:
            raise GabrielError("INVALID_ARGUMENT", "presence.watch_profiles: user_ids must be a non-empty list")
        ids = list(dict.fromkeys(assert_user_id(u) for u in user_ids))
        self._assert_profile_cap(ids)
        self._retain_profiles(ids)
        return SubscriptionHandle(ids, lambda: self._release_profiles(ids))

    def _assert_profile_cap(self, ids: list[str]) -> None:
        size = len(set(self._profile_refs) | set(ids))
        if size > PROFILE_POOL_CAP:
            raise GabrielError(
                "SUBSCRIPTION_CAP_EXCEEDED",
                f"profile/DND subscriptions would reach {size} users (> {PROFILE_POOL_CAP})",
            )

    def _retain_profiles(self, ids: list[str]) -> None:
        for id in ids:
            self._profile_refs[id] = self._profile_refs.get(id, 0) + 1
        self._schedule_profile()

    def _release_profiles(self, ids: list[str]) -> None:
        for id in ids:
            p = self._profile_refs.get(id, 1) - 1
            if p <= 0:
                self._profile_refs.pop(id, None)
                self._baseline.pop(id, None)
            else:
                self._profile_refs[id] = p
        self._schedule_profile()

    async def get_status(self, user_id: str) -> PresenceStatus:
        """Cached presence + profile status + DND for a user; fetches what is missing."""
        id = assert_user_id(user_id)
        entry = self._cache.get(id)
        presence: PresenceValue = entry.presence if entry is not None else "unknown"

        async def load_user() -> UserRecord | None:
            try:
                return await self._hydrator.users.load(id)
            except Exception:
                return None

        async def load_dnd() -> DndInfo | None:
            if id in self._last_dnd:
                return self._last_dnd[id]
            try:
                return (await self._hydrator.fetch_dnd([id])).get(id)
            except Exception:
                return None

        user, dnd = await asyncio.gather(load_user(), load_dnd())
        if dnd is not None:
            self._last_dnd[id] = dnd
        return PresenceStatus(presence=presence, status=user.status if user is not None else None, dnd=dnd)

    @property
    def watched_user_ids(self) -> list[str]:
        return list(self._presence_refs)

    def is_profile_tracked(self, user_id: str) -> bool:
        return user_id in self._profile_refs

    def on_hello(self, epoch: int) -> None:
        """New connection epoch: the gateway kept nothing, so replay both pools."""
        self._epoch = epoch
        self._presence_applied = set()
        self._profile_applied = set()
        self._dnd_applied = set()
        self._profile_full = False
        if self._presence_refs:
            self._flush_presence()
        if self._profile_refs:
            self._schedule_profile()

    def handle_frame(self, frame: RawWsFrame) -> bool:
        """Handles presence-domain frames. Returns True when consumed."""
        t = frame.get("type")
        if t == "presence_change":
            presence: Literal["active", "away"] = "active" if frame.get("presence") == "active" else "away"
            users = frame.get("users")
            if not isinstance(users, list):
                users = [frame["user"]] if isinstance(frame.get("user"), str) else []
            for u in users:
                if isinstance(u, str):
                    self._apply_presence(u, presence)
            return True
        if t == "manual_presence_change":
            self._apply_presence(self._core.session.user_id, "active" if frame.get("presence") == "active" else "away")
            return True
        if t in ("user_change", "user_invalidated", "user_status_changed"):
            user = frame.get("user")
            id = user if isinstance(user, str) else user.get("id") if isinstance(user, dict) else None
            # Subscribed users always; any other user only when there is a cached record to diff against.
            if isinstance(id, str) and (id in self._profile_refs or self._hydrator.users.peek(id) is not None):
                spawn(self._refresh_profile(id, user if isinstance(user, dict) else None), self._tasks)
            return True
        if t in ("dnd_updated", "dnd_updated_user", "dnd_invalidated"):
            user = frame.get("user")
            if isinstance(user, str):
                id = user
            elif isinstance(user, dict):
                id = user.get("id")
            else:
                id = self._core.session.user_id
            if isinstance(id, str) and id in self._dnd_refs:
                spawn(self._refresh_dnd(id), self._tasks)
            return True
        return False

    def _apply_presence(self, user_id: str, presence: Literal["active", "away"]) -> None:
        prev = self._cache.get(user_id)
        self._cache[user_id] = _PresenceEntry(presence, self._epoch, time.time())
        if user_id not in self._presence_refs:
            return
        if prev is None or prev.presence == "unknown":
            self._emit_presence(user_id, presence, prev.presence if prev is not None else "unknown", True)
            return
        if prev.presence == presence:
            return  # reconnect snapshot of an unchanged user
        self._emit_presence(user_id, presence, prev.presence, False)

    def _emit_presence(
        self, user_id: str, presence: Literal["active", "away"], previous: PresenceValue, initial: bool
    ) -> None:
        self._seq += 1
        self._emit(
            PresenceChangedEvent(
                event_id=f"presence:{user_id}:{self._epoch}:{self._seq}",
                user=_brief(self._hydrator.users.peek(user_id), user_id),
                presence=presence,
                previous_presence=previous,
                is_initial_snapshot=initial,
            ),
            user_id,
        )

    def _schedule_presence(self) -> None:
        if self._presence_timer is not None:
            self._presence_timer.cancel()
        self._presence_timer = asyncio.get_running_loop().call_later(_PRESENCE_DEBOUNCE, self._flush_presence)

    def _flush_presence(self) -> None:
        """Sends the full desired set (overwrite semantics, fire-and-forget)."""
        if self._presence_timer is not None:
            self._presence_timer.cancel()
            self._presence_timer = None
        desired = set(self._presence_refs)
        if desired == self._presence_applied:
            return
        if not self._core.ws.is_connected():
            return  # replayed on next hello
        ids = list(self._presence_refs)
        if presence_frame_bytes(ids) > PRESENCE_FRAME_MAX_BYTES:
            self._logger.error({"count": len(ids)}, "presence_sub exceeds frame cap; not sent")
            return
        try:
            self._core.send_ws_frame({"type": "presence_sub", "ids": ids})
        except Exception as err:
            self._logger.debug({"err": str(err)}, "presence_sub deferred until reconnect")
            return
        epoch = self._epoch
        fresh = [id for id in ids if id not in self._presence_applied]
        self._presence_applied = desired

        def snapshot_timeout() -> None:
            self._timers.discard(handle)
            if epoch != self._epoch:
                return
            for id in fresh:
                e = self._cache.get(id)
                if e is None or e.epoch != epoch:
                    self._cache[id] = _PresenceEntry("unknown", epoch, time.time())

        handle = asyncio.get_running_loop().call_later(_SNAPSHOT_TIMEOUT, snapshot_timeout)
        self._timers.add(handle)

    def _schedule_profile(self) -> None:
        self._profile_dirty = True
        if self._profile_running is not None:
            return

        async def run() -> None:
            try:
                while self._profile_dirty:
                    self._profile_dirty = False
                    await self._reconcile_profile()
            except Exception as err:
                self._logger.warn({"err": str(err)}, "profile subscription reconcile failed")
            finally:
                self._profile_running = None

        self._profile_running = spawn(run(), self._tasks)

    async def _reconcile_profile(self) -> None:
        """Incremental diff, 100 ids per frame, each frame ACKed before ``applied`` advances."""
        if not self._core.ws.is_connected():
            return
        epoch = self._epoch
        remove_profile = [id for id in self._profile_applied if id not in self._profile_refs]
        for batch in chunk(remove_profile, _PROFILE_FRAME_BATCH):
            await self._send_subscription("user_change_unsubscribe_request", batch)
            if epoch != self._epoch:
                return
            self._profile_applied.difference_update(batch)
        remove_dnd = [id for id in self._dnd_applied if id not in self._dnd_refs]
        for batch in chunk(remove_dnd, _PROFILE_FRAME_BATCH):
            await self._send_subscription("dnd_invalidated_unsubscribe_request", batch)
            if epoch != self._epoch:
                return
            self._dnd_applied.difference_update(batch)
        if self._profile_full:
            return
        add_profile = [id for id in self._profile_refs if id not in self._profile_applied]
        for batch in chunk(add_profile, _PROFILE_FRAME_BATCH):
            ok = await self._send_subscription("user_change_subscribe_request", batch)
            if not ok or epoch != self._epoch:
                return
            self._profile_applied.update(batch)
            # Prime the baseline so the first change can be diffed against it.
            spawn(self._prime_users(batch), self._tasks)
        # DND subscriptions follow the user_change ones they belong to (a refused batch stops both).
        add_dnd = [id for id in self._dnd_refs if id not in self._dnd_applied and id in self._profile_applied]
        for batch in chunk(add_dnd, _PROFILE_FRAME_BATCH):
            await self._send_subscription("dnd_invalidated_subscribe_request", batch)
            if epoch != self._epoch:
                return
            self._dnd_applied.update(batch)
            spawn(self._prime_dnd(batch), self._tasks)

    async def _prime_users(self, batch: list[str]) -> None:
        try:
            records = await self._hydrator.users.load_many(batch)
        except Exception:
            return
        for rec in records:
            if rec is not None and rec.id in self._profile_refs:
                self._baseline.setdefault(rec.id, rec)

    async def _prime_dnd(self, batch: list[str]) -> None:
        try:
            self._last_dnd.update(await self._hydrator.fetch_dnd(batch))
        except Exception:
            pass

    async def _send_subscription(self, subtype: str, ids: list[str], retried: bool = False) -> bool:
        try:
            await self._core.send_ws_command({"type": "subscription_update", "subtype": subtype, "ids": ids})
            return True
        except Exception as err:
            gw = _gateway_error_code(err)
            if gw == _GATEWAY_ID_CAP:
                self._profile_full = True
                self._logger.warn(
                    {"subtype": subtype}, "gateway refused profile subscriptions: per-connection cap reached"
                )
                return False
            if gw == _GATEWAY_RATE_LIMIT and not retried:
                await asyncio.sleep(10.0)
                return await self._send_subscription(subtype, ids, True)
            if isinstance(err, GabrielError) and err.code == "NETWORK_TIMEOUT":
                # Some subscription subtypes are not ACKed; treat as applied and move on.
                self._logger.debug({"subtype": subtype}, "subscription_update not acknowledged; assuming applied")
                return True
            raise

    async def _refresh_profile(self, user_id: str, inline: dict[str, Any] | None = None) -> None:
        """Refreshes a user's record after ``user_change`` / ``user_invalidated`` / ``user_status_changed``.

        A frame carrying a full user object (``name`` + ``profile``) is used as is, anything else is
        re-fetched. The hydrator cache is updated before any event is emitted.
        """
        before = self._hydrator.users.peek(user_id) or self._baseline.get(user_id)
        try:
            after: UserRecord | None
            if inline is not None and isinstance(inline.get("profile"), dict) and isinstance(inline.get("name"), str):
                after = to_user_record({**inline, "id": user_id}, self._core.session.team_id)
                self._hydrator.users.prime(user_id, after)
            else:
                after = await self._hydrator.users.load(user_id, fresh=True)
            if after is None:
                return
            subscribed = user_id in self._profile_refs
            if subscribed:
                self._baseline[user_id] = after
            if before is not None:
                self._emit_profile_change(user_id, before, after)
            if not subscribed:
                return
            prev = before.status if before is not None else None
            cur = after.status
            if prev is not None and (prev.emoji, prev.text, prev.expiration) == (cur.emoji, cur.text, cur.expiration):
                return
            self._seq += 1
            self._emit(
                UserStatusChangedEvent(
                    event_id=f"user_status:{user_id}:{self._seq}:{int(time.time() * 1000)}",
                    user=_brief(after, user_id),
                    current_status=cur,
                    previous_status=prev,
                ),
                user_id,
            )
        except Exception as err:
            self._logger.debug({"err": str(err), "user_id": user_id}, "profile refetch failed")

    def _emit_profile_change(self, user_id: str, before: UserRecord, after: UserRecord) -> None:
        changed = profile_changes(before, after)
        if not changed:
            return
        names = dict(_PROFILE_FIELDS)
        previous = UserProfilePrevious(**{names[f]: getattr(before, names[f]) for f in changed})
        self._seq += 1
        self._emit(
            UserProfileChangedEvent(
                event_id=f"user_profile:{user_id}:{self._seq}:{int(time.time() * 1000)}",
                user=UserProfileRef(
                    id=user_id,
                    name=after.name or None,
                    display_name=after.display_name or None,
                    real_name=after.real_name or None,
                    handle=after.handle or None,
                    is_bot=after.is_bot,
                    is_external=after.is_external,
                ),
                changed=changed,
                previous=previous,
            ),
            user_id,
        )

    async def _refresh_dnd(self, user_id: str) -> None:
        try:
            cur = (await self._hydrator.fetch_dnd([user_id])).get(user_id)
            if cur is None:
                return
            prev = self._last_dnd.get(user_id)
            self._last_dnd[user_id] = cur
            if prev is not None and (prev.enabled, prev.next_start_ts, prev.next_end_ts) == (
                cur.enabled,
                cur.next_start_ts,
                cur.next_end_ts,
            ):
                return
            self._seq += 1
            self._emit(
                UserDndChangedEvent(
                    event_id=f"user_dnd:{user_id}:{self._seq}:{int(time.time() * 1000)}",
                    user=_brief(self._hydrator.users.peek(user_id), user_id),
                    dnd=cur,
                    previous_dnd=prev,
                ),
                user_id,
            )
        except Exception as err:
            self._logger.debug({"err": str(err), "user_id": user_id}, "dnd refetch failed")

    def stop(self) -> None:
        if self._presence_timer is not None:
            self._presence_timer.cancel()
            self._presence_timer = None
        for h in self._timers:
            h.cancel()
        self._timers.clear()
