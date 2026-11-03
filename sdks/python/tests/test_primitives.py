"""Port of the validation / redaction / error-mapping / PrioritySemaphore / MemoryStateStore cases of
sdks/node/test/primitives.test.ts (the client-layer primitives are tested with the client)."""

from __future__ import annotations

import asyncio

import pytest

from gabriel.errors import GabrielError, from_slack_error
from gabriel.state.store import MemoryStateStore
from gabriel.util.redact import redact_secret, scrub_secrets
from gabriel.util.semaphore import PrioritySemaphore
from gabriel.util.validate import compare_ts, normalize_emoji
from harness import tick

# -- validation helpers ------------------------------------------------------------------------


def test_compares_slack_timestamps_numerically() -> None:
    assert compare_ts("1800000000.000100", "1800000000.000099") > 0
    assert compare_ts("999999999.9", "1000000000.0") < 0
    assert compare_ts("1.5", "1.500000") == 0


def test_normalizes_emoji_names_and_rejects_garbage() -> None:
    assert normalize_emoji(":eyes:") == "eyes"
    with pytest.raises(GabrielError):
        normalize_emoji("<script>")


# -- redaction ---------------------------------------------------------------------------------


def test_never_prints_the_secret_body() -> None:
    token = "xoxc-test-123456-abcdefabcdef-abcdefabcdefabcd"
    assert redact_secret(token) == "xoxc-test-...[REDACTED, len=46]"
    assert "abcdefabcdef" not in scrub_secrets(f"wss://x/?token={token}&frt=abc d=xoxd-zzz")
    assert "SECRET" not in scrub_secrets("frt=SECRET")


# -- error mapping -----------------------------------------------------------------------------


def test_maps_slack_errors_to_codes_with_retryability() -> None:
    assert from_slack_error("chat.postMessage", "channel_not_found").code == "CHANNEL_NOT_FOUND"
    assert from_slack_error("x", "invalid_auth").code == "TOKEN_EXPIRED"
    assert from_slack_error("x", "invalid_auth").retryable is True
    unknown = from_slack_error("x", "weird_error")
    assert unknown.code == "SLACK_API_ERROR"
    assert unknown.raw_slack_error == "weird_error"


# -- PrioritySemaphore -------------------------------------------------------------------------


async def test_reserves_a_slot_for_interactive_work() -> None:
    sem = PrioritySemaphore(2, 1)
    r1 = await sem.acquire("normal")
    normal_granted = False

    async def wait_normal() -> None:
        nonlocal normal_granted
        await sem.acquire("normal")
        normal_granted = True

    task = asyncio.ensure_future(wait_normal())
    r2 = await sem.acquire("interactive")
    await tick()
    assert normal_granted is False
    r1()
    await tick()
    # Normal work may only use total - reserved slots, so it waits for the interactive slot too.
    assert normal_granted is False
    r2()
    await tick()
    assert normal_granted is True
    await task


# -- MemoryStateStore --------------------------------------------------------------------------


async def test_max_merges_timestamps_monotonically() -> None:
    s = MemoryStateStore()
    assert await s.max_merge_ts("k", "1800000002.000100") == "1800000002.000100"
    assert await s.max_merge_ts("k", "1800000001.000100") == "1800000002.000100"
    assert await s.set_if_absent("lock", "a") is True
    assert await s.set_if_absent("lock", "b") is False
