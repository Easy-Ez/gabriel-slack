from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest

# Make ``import harness`` work regardless of pytest's import mode.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gabriel.core.auth import AuthOptions
from gabriel.core.core import GabrielCore
from gabriel.core.ws import WsTimings
from harness import COOKIE, ENDPOINTS, FakeSlack

#: Short protocol timings so lifecycle tests stay fast and deterministic.
FAST_TIMINGS = WsTimings(backoff_base=0.01, backoff_cap=0.05)


@pytest.fixture
async def slack() -> AsyncIterator[FakeSlack]:
    fake = FakeSlack()
    yield fake
    await fake.aclose()


@pytest.fixture
async def make_core() -> AsyncIterator[Callable[..., GabrielCore]]:
    """Factory for cores wired to a FakeSlack (Node: ``makeCore``); every core is closed afterwards."""
    cores: list[GabrielCore] = []

    def factory(fake: FakeSlack, **overrides: Any) -> GabrielCore:
        options: dict[str, Any] = {
            "auth": AuthOptions(cookie_d=COOKIE),
            "endpoints": ENDPOINTS,
            "http_client": fake.http_client,
            "ws_factory": fake.create_web_socket,
            "max_http_retries": 2,
            "ws_timings": FAST_TIMINGS,
            **overrides,
        }
        core = GabrielCore(**options)
        cores.append(core)
        return core

    yield factory
    for core in cores:
        await core.close()
