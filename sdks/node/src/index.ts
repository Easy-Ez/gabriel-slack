/**
 * gabriel-slack — typed, AI-friendly real-time SDK for the Slack Web client protocol.
 *
 * High level: {@link GabrielClient} (ordered hydrated events, domain APIs, AI sandbox).
 * Low level:  {@link GabrielCore} (raw frames, Web API / EdgeAPI / WebSocket primitives).
 */
export { GabrielClient } from "./client/client.js";
export type {
  ConcurrencyOptions,
  ErrorHandler,
  GabrielClientOptions,
  Handler,
  HealthReport,
  HydrationOptions,
  LoopGuardOptions,
  Middleware,
  RecoveryOptions,
} from "./client/client.js";
export type { EventContext, MessageMatcher, ReplyOptions, WaitForReplyOptions } from "./client/context.js";
export type { ChatSchedulerOptions, CreateStreamOptions, SendOptions, SendResult, UpdateOptions } from "./client/chat.js";
export type { StreamController } from "./client/stream.js";
export type { PresenceStatus, SubscriptionHandle } from "./client/presence.js";
export { PRESENCE_FRAME_MAX_BYTES, PROFILE_POOL_CAP } from "./client/presence.js";
export type {
  FileInfo,
  HistoryMessage,
  SearchMessageHit,
  SearchOptions,
  SearchPage,
  ThreadContext,
  ThreadMessage,
  UploadOptions,
} from "./client/apis.js";
export { ScopedClient } from "./client/scope.js";
export type { ScopePolicy, ScopedWriteAction } from "./client/scope.js";
export { toLLMContext } from "./client/llm-context.js";

export { GabrielCore, KNOWN_FRAME_TYPES } from "./core/core.js";
export type { GabrielCoreOptions, ProtocolDriftInfo } from "./core/core.js";
export type { AuthOptions, AuthTeam, AuthState, CookieExchangeResult } from "./core/auth.js";
export type { ClientMetadata, Endpoints } from "./core/metadata.js";
export { DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS } from "./core/metadata.js";
export type { FetchLike, MetricsSink, RawMessageFrame, RawWsFrame, RequestOpts, WebSocketFactory, WebSocketLike } from "./core/types.js";
export type { WsState } from "./core/ws.js";

export { GabrielError, isGabrielError } from "./errors.js";
export type { GabrielErrorCode } from "./errors.js";

export * from "./events/types.js";
export { normalizeFrame, laneKeyOf, contentHash } from "./events/normalize.js";
export type { Draft, NormalizeContext } from "./events/normalize.js";

export { convertBlocksToMarkdown, mrkdwnToMarkdown } from "./markdown/blocks-to-markdown.js";
export type { EntityResolver } from "./markdown/blocks-to-markdown.js";
export { chunkMarkdown, measureMarkdown, healOpenFence, DEFAULT_CHUNK_BUDGET } from "./markdown/chunker.js";

export { MemoryStateStore } from "./state/store.js";
export type { StateStore } from "./state/store.js";
export { FileStateStore } from "./state/file.js";
export type { FileStateStoreOptions } from "./state/file.js";

export { consoleLogger, silentLogger } from "./util/logger.js";
export type { Logger } from "./util/logger.js";
export { redactSecret } from "./util/redact.js";
export { loadSessionCookie } from "./cli/session-store.js";
