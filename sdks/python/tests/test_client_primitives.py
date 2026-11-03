"""Unit tests for the client-layer building blocks (executor, loader, origin, presence, stores)."""

from __future__ import annotations

import asyncio
import json
import random
import stat
from pathlib import Path

import pytest

from gabriel.client.executor import JobHandle, KeyedExecutor
from gabriel.client.loader import BatchLoader
from gabriel.client.origin import LoopBreaker, SelfRegistry
from gabriel.client.presence import PRESENCE_FRAME_MAX_BYTES, presence_frame_bytes
from gabriel.errors import GabrielError
from gabriel.state.file import FileStateStore


async def tick(seconds: float = 0) -> None:
    await asyncio.sleep(seconds)


class TestKeyedExecutor:
    async def test_same_key_serial_in_order_other_keys_parallel(self) -> None:
        ex: KeyedExecutor[int] = KeyedExecutor(global_concurrency=4, per_key_concurrency=1, max_queue=100)
        log: list[str] = []
        concurrent_a = 0
        max_concurrent_a = 0

        def make(i: int):  # type: ignore[no-untyped-def]
            async def job(_h: JobHandle) -> None:
                nonlocal concurrent_a, max_concurrent_a
                concurrent_a += 1
                max_concurrent_a = max(max_concurrent_a, concurrent_a)
                await tick(random.random() * 0.005)
                log.append(f"A{i}")
                concurrent_a -= 1

            return job

        for i in range(5):
            ex.submit("A", i, make(i))

        async def b(_h: JobHandle) -> None:
            log.append("B0")

        ex.submit("B", 0, b)
        await ex.idle()
        assert max_concurrent_a == 1
        assert [entry for entry in log if entry.startswith("A")] == ["A0", "A1", "A2", "A3", "A4"]
        assert log.index("B0") < log.index("A4")

    async def test_rejects_submissions_beyond_max_queue(self) -> None:
        ex: KeyedExecutor[int] = KeyedExecutor(global_concurrency=1, per_key_concurrency=1, max_queue=2)
        block = asyncio.get_running_loop().create_future()

        async def blocked(_h: JobHandle) -> None:
            await block

        async def noop(_h: JobHandle) -> None:
            pass

        assert ex.submit("k", 0, blocked) is True
        assert ex.submit("k", 1, noop) is True
        assert ex.submit("k", 2, noop) is True
        assert ex.submit("k", 3, noop) is False
        block.set_result(None)
        await ex.idle()

    async def test_suspended_job_yields_and_resumes_ahead_of_queue(self) -> None:
        ex: KeyedExecutor[str] = KeyedExecutor(global_concurrency=1, per_key_concurrency=1, max_queue=100)
        order: list[str] = []
        reply: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def waiter(h: JobHandle) -> None:
            order.append("waiter:start")
            h.suspend()
            await reply
            await h.resume()
            order.append("waiter:resumed")

        async def nxt(_h: JobHandle) -> None:
            order.append("next")
            reply.set_result(None)
            await tick(0.005)
            order.append("next:end")

        async def later(_h: JobHandle) -> None:
            order.append("later")

        ex.submit("k", "waiter", waiter)
        ex.submit("k", "next", nxt)
        ex.submit("k", "later", later)
        await ex.idle()
        assert order == ["waiter:start", "next", "next:end", "waiter:resumed", "later"]

    async def test_no_permit_leak_on_double_resume(self) -> None:
        ex: KeyedExecutor[str] = KeyedExecutor(global_concurrency=2, per_key_concurrency=1, max_queue=100)
        gate: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def waiter(h: JobHandle) -> None:
            h.suspend()
            await gate
            await asyncio.gather(h.resume(), h.resume())

        async def blocker(_h: JobHandle) -> None:
            gate.set_result(None)
            await tick(0.01)

        ran: list[str] = []

        async def after(_h: JobHandle) -> None:
            ran.append("after")

        ex.submit("C1", "waiter", waiter)
        ex.submit("C1", "blocker", blocker)
        ex.submit("C1", "after", after)
        await ex.idle()
        assert ran == ["after"]

        async def again(_h: JobHandle) -> None:
            ran.append("again")

        ex.submit("C1", "again", again)
        await ex.idle()
        assert ran == ["after", "again"]

    async def test_extracts_pending_jobs_by_predicate(self) -> None:
        ex: KeyedExecutor[str] = KeyedExecutor(global_concurrency=1, per_key_concurrency=1, max_queue=100)
        block: asyncio.Future[None] = asyncio.get_running_loop().create_future()

        async def running(_h: JobHandle) -> None:
            await block

        async def noop(_h: JobHandle) -> None:
            pass

        ex.submit("k", "running", running)
        ex.submit("k", "a", noop)
        ex.submit("k", "b", noop)
        await tick()
        assert ex.extract_pending("k", lambda m: m == "b") == (True, "b")
        assert ex.queue_depth == 1
        block.set_result(None)
        await ex.idle()


class TestBatchLoader:
    async def test_batches_shares_inflight_and_caches_misses(self) -> None:
        batches: list[list[str]] = []

        async def batch_fn(ids: list[str]) -> dict[str, str]:
            batches.append(ids)
            return {id: id.upper() for id in ids if id != "missing"}

        loader: BatchLoader[str] = BatchLoader(batch_fn, window=0.005)
        results = await asyncio.gather(*(loader.load(id) for id in ["a", "b", "a", "missing"]))
        assert results == ["A", "B", "A", None]
        assert batches == [["a", "b", "missing"]]
        assert await loader.load("missing") is None
        assert len(batches) == 1
        assert loader.is_known("missing") and loader.peek("missing") is None
        assert loader.peek("a") == "A"

    async def test_does_not_cache_transient_errors(self) -> None:
        fail = True

        async def batch_fn(ids: list[str]) -> dict[str, str]:
            if fail:
                raise GabrielError("NETWORK_TIMEOUT", "boom")
            return {id: "ok" for id in ids}

        loader: BatchLoader[str] = BatchLoader(batch_fn, window=0.001)
        with pytest.raises(GabrielError) as info:
            await loader.load("x")
        assert info.value.code == "NETWORK_TIMEOUT"
        fail = False
        assert await loader.load("x") == "ok"

    async def test_merges_300_lookups_into_at_most_3_requests(self) -> None:
        requests = 0

        async def batch_fn(ids: list[str]) -> dict[str, str]:
            nonlocal requests
            requests += 1
            return {id: id for id in ids}

        loader: BatchLoader[str] = BatchLoader(batch_fn)
        await asyncio.gather(*(loader.load(f"U{i}") for i in range(300)))
        assert requests <= 3


def test_self_registry_classifies_origins() -> None:
    reg = SelfRegistry(lambda: "U0SELF00001")
    reg.register_client_msg_id("cmid-1")
    me = "U0SELF00001"
    assert reg.classify(user=me, client_msg_id="cmid-1", ts="1.1", content_hash="h", is_edit=False) == "this_client"
    assert reg.classify(user=me, ts="1.2", content_hash="h", is_edit=False) == "self_other_client"
    assert reg.classify(user="U0OTHER0001", ts="1.3", content_hash="h", is_edit=False) == "other_user"
    assert reg.classify(user="U0OTHER0001", bot_id="B1", ts="1.4", content_hash="h", is_edit=False) == "bot"
    reg.register_sent_ts("1.5")
    reg.register_written_hash("1.5", "sdk-hash")
    assert reg.classify(user=me, ts="1.5", content_hash="sdk-hash", is_edit=True) == "this_client"
    assert reg.classify(user=me, ts="1.5", content_hash="manual-edit", is_edit=True) == "self_other_client"


def test_loop_breaker_opens_after_n_writes_per_minute_per_thread() -> None:
    now = 0.0
    lb = LoopBreaker(3, lambda: now)
    for _ in range(3):
        assert lb.try_record("C1", "1.0").allowed
    check = lb.try_record("C1", "1.0")
    assert (check.allowed, check.count) == (False, 3)
    assert lb.try_record("C1", "2.0").allowed
    now = 61.0
    assert lb.try_record("C1", "1.0").allowed


def test_presence_cap_is_enforced_by_serialized_bytes() -> None:
    def ids(n: int) -> list[str]:
        return [f"U{str(i).zfill(10)}" for i in range(n)]

    assert presence_frame_bytes(ids(1100)) <= PRESENCE_FRAME_MAX_BYTES
    assert presence_frame_bytes(ids(1200)) > PRESENCE_FRAME_MAX_BYTES


async def test_file_state_store_persists_with_0600_and_node_format(tmp_path: Path) -> None:
    store = FileStateStore(tmp_path / "state", flush_interval=0.01)
    await store.set("a", "1")
    await store.set("ttl", "x", ttl=3600)
    assert await store.max_merge_ts("wm", "1700000000.000200") == "1700000000.000200"
    assert await store.max_merge_ts("wm", "1700000000.000100") == "1700000000.000200"
    await store.close()
    file = tmp_path / "state" / "state.json"
    assert stat.S_IMODE(file.stat().st_mode) == 0o600
    raw = json.loads(file.read_text())
    assert raw["a"] == {"value": "1"}
    assert isinstance(raw["ttl"]["expiresAt"], int)
    reopened = FileStateStore(tmp_path / "state")
    assert reopened.persistent is True
    assert await reopened.get("wm") == "1700000000.000200"
    assert await reopened.get("ttl") == "x"
    assert sorted(await reopened.keys("")) == ["a", "ttl", "wm"]


def test_file_state_store_rejects_bad_dir() -> None:
    with pytest.raises(GabrielError) as info:
        FileStateStore("")
    assert info.value.code == "INVALID_ARGUMENT"
