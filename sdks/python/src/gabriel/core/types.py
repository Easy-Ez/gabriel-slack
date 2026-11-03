"""Transport-level types shared by the core and the client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from ..util.aio import AbortSignal

#: An unmodified downstream WebSocket frame (decoded JSON object).
RawWsFrame = dict[str, Any]

RetryPolicy = Literal["auto", "safe", "none"]


@dataclass(slots=True)
class RequestOpts:
    #: Per-request timeout in seconds. Web API default 15 s, EdgeAPI default 10 s.
    timeout: float | None = None
    #: Cancels the request (raises CANCELLED).
    signal: AbortSignal | None = None
    #: ``auto`` (default) retries read-only and idempotent methods; ``none`` disables retries
    #: other than 429 cool-downs.
    retry: RetryPolicy | None = None
    #: Value for the ``_x_reason`` telemetry field.
    reason: str | None = None


class WebSocketLike(Protocol):
    """Minimal async WebSocket surface the SDK needs.

    Matches :class:`websockets.asyncio.client.ClientConnection`: ``recv()`` returns the next
    text frame and raises when the connection is closed; ``close_code`` is the close code once
    closed.
    """

    async def send(self, data: str) -> None: ...
    async def recv(self) -> str | bytes: ...
    async def close(self, code: int = 1000, reason: str = "") -> None: ...


#: Opens a WebSocket to ``url`` with the given request headers. Raises on handshake failure;
#: an HTTP rejection should raise an exception carrying ``status_code`` when possible.
WebSocketFactory = Callable[[str, dict[str, str]], Awaitable[WebSocketLike]]


class MetricsSink(Protocol):
    """Observability hook. Label values are IDs or enums, never message content or credentials."""

    def increment(self, name: str, labels: dict[str, str] | None = None) -> None: ...
    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None: ...


class _NoopMetrics:
    def increment(self, name: str, labels: dict[str, str] | None = None) -> None:
        pass

    def observe(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        pass


noop_metrics: MetricsSink = _NoopMetrics()
