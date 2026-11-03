"""gabriel-slack — typed, AI-friendly real-time SDK for the Slack Web client protocol.

High level: :class:`GabrielClient` (ordered hydrated events, domain APIs, AI sandbox).
Low level:  :class:`GabrielCore` (raw frames, Web API / EdgeAPI / WebSocket primitives).
"""

from importlib.metadata import PackageNotFoundError, version

from .cli.session_store import SessionCookie, load_session_cookie
from .client.apis import (
    FileInfo,
    HistoryMessage,
    HistoryPage,
    SearchMessageHit,
    SearchPage,
    ThreadContext,
    ThreadMessage,
)
from .client.chat import ChatSchedulerOptions, SendResult, UpdateResult
from .client.client import (
    AdvancedOptions,
    ConcurrencyOptions,
    EventContextImpl,
    EventStream,
    GabrielClient,
    HealthReport,
    HydrationOptions,
    LoopGuardOptions,
    RecoveryOptions,
)
from .client.context import EventContext, MessageMatcher, ReplyOptions
from .client.llm_context import to_llm_context
from .client.presence import PRESENCE_FRAME_MAX_BYTES, PROFILE_POOL_CAP, PresenceStatus, SubscriptionHandle
from .client.scope import ReadPolicy, ScopedClient, ScopedWriteAction, ScopePolicy, WritePolicy
from .client.stream import StreamController, StreamResult
from .core.auth import AuthOptions, AuthTeam, CookieExchangeResult, WorkspaceSession
from .core.core import KNOWN_FRAME_TYPES, GabrielCore, ProtocolDriftInfo
from .core.metadata import DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS, ClientMetadata, Endpoints
from .core.types import MetricsSink, RawWsFrame, RequestOpts, WebSocketFactory, WebSocketLike
from .core.ws import WsState
from .errors import GabrielError, GabrielErrorCode, is_gabriel_error
from .events.normalize import Draft, NormalizeContext, content_hash, lane_key_of, normalize_frame
from .events.types import *  # noqa: F403
from .events.types import __all__ as _event_names
from .markdown.blocks_to_markdown import EntityResolver, convert_blocks_to_markdown, mrkdwn_to_markdown
from .markdown.chunker import DEFAULT_CHUNK_BUDGET, chunk_markdown, heal_open_fence, measure_markdown
from .state.file import FileStateStore
from .state.store import MemoryStateStore, StateStore
from .util.aio import AbortController, AbortSignal
from .util.logger import Logger, console_logger, silent_logger, stdlib_logger
from .util.redact import redact_secret

try:
    __version__ = version("gabriel-slack")
except PackageNotFoundError:  # pragma: no cover - running from a source tree without metadata
    __version__ = "0.0.0"

__all__ = [
    "DEFAULT_CHUNK_BUDGET",
    "DEFAULT_CLIENT_METADATA",
    "DEFAULT_ENDPOINTS",
    "KNOWN_FRAME_TYPES",
    "PRESENCE_FRAME_MAX_BYTES",
    "PROFILE_POOL_CAP",
    "AbortController",
    "AbortSignal",
    "AdvancedOptions",
    "AuthOptions",
    "AuthTeam",
    "ChatSchedulerOptions",
    "ClientMetadata",
    "ConcurrencyOptions",
    "CookieExchangeResult",
    "Draft",
    "Endpoints",
    "EntityResolver",
    "EventContext",
    "EventContextImpl",
    "EventStream",
    "FileInfo",
    "FileStateStore",
    "GabrielClient",
    "GabrielCore",
    "GabrielError",
    "GabrielErrorCode",
    "HealthReport",
    "HistoryMessage",
    "HistoryPage",
    "HydrationOptions",
    "Logger",
    "LoopGuardOptions",
    "MemoryStateStore",
    "MessageMatcher",
    "MetricsSink",
    "NormalizeContext",
    "PresenceStatus",
    "ProtocolDriftInfo",
    "RawWsFrame",
    "ReadPolicy",
    "RecoveryOptions",
    "ReplyOptions",
    "RequestOpts",
    "ScopePolicy",
    "ScopedClient",
    "ScopedWriteAction",
    "SearchMessageHit",
    "SearchPage",
    "SendResult",
    "SessionCookie",
    "StateStore",
    "StreamController",
    "StreamResult",
    "SubscriptionHandle",
    "ThreadContext",
    "ThreadMessage",
    "UpdateResult",
    "WebSocketFactory",
    "WebSocketLike",
    "WorkspaceSession",
    "WritePolicy",
    "WsState",
    "__version__",
    "chunk_markdown",
    "console_logger",
    "content_hash",
    "convert_blocks_to_markdown",
    "heal_open_fence",
    "is_gabriel_error",
    "lane_key_of",
    "load_session_cookie",
    "measure_markdown",
    "mrkdwn_to_markdown",
    "normalize_frame",
    "redact_secret",
    "silent_logger",
    "stdlib_logger",
    "to_llm_context",
    *_event_names,
]
