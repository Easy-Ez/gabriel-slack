import { GabrielError } from "../errors.js";
import type { ChannelRef, DndInfo, UserRef } from "../events/types.js";
import type { Logger } from "../util/logger.js";
import type { ChannelsApi, FileInfo, FilesApi, HistoryMessage, ReactionsApi, SearchApi, SearchMessageHit, SearchOptions, SearchPage, ThreadContext, ThreadMessage, ThreadsApi, UploadOptions, UsersApi } from "./apis.js";
import type { ChatApi, CreateStreamOptions, SendOptions, SendResult, UpdateOptions } from "./chat.js";
import { currentTrigger } from "./context.js";
import type { SelfRegistry } from "./origin.js";
import type { PresenceApi, PresenceStatus } from "./presence.js";
import type { StreamController } from "./stream.js";

export type ScopedWriteAction =
  | "chat.send"
  | "chat.createStream"
  | "chat.update"
  | "chat.delete"
  | "reactions.add"
  | "reactions.remove"
  | "files.upload"
  | "files.delete";

export interface ScopePolicy {
  read?: {
    /** Readable channel allow-list (history, thread context, file downloads, search hits). */
    channels?: string[];
    allowSearch?: boolean;
    maxFileDownloadBytes?: number;
  };
  write?: {
    /** Only reply / react in the channel + thread that triggered the running handler. */
    targetScope?: "trigger_thread_only";
    allowedActions?: ScopedWriteAction[];
    /** Appended to every written message (AI-generated content disclosure). */
    disclosureSuffix?: string;
  };
  /** Human approval hook, consulted for every write; return false to refuse. */
  approval?: (action: string, params: unknown) => Promise<boolean>;
}

interface Target {
  channel?: string;
  threadTs?: string;
  ts?: string;
}

function violation(message: string): GabrielError {
  return new GabrielError("SCOPE_POLICY_VIOLATION", message);
}

/**
 * Least-privilege view over a client for LLM tool use (spec/behavior.md §11). Same API
 * surface; every out-of-policy call throws SCOPE_POLICY_VIOLATION and is audited.
 * The escape hatch (`core`) is not available on scoped views.
 */
export class ScopedClient {
  readonly chat: {
    send(opts: SendOptions): Promise<SendResult>;
    update(opts: UpdateOptions): Promise<{ channel: string; ts: string }>;
    delete(opts: { channel: string; ts: string }): Promise<void>;
    createStream(opts: CreateStreamOptions): Promise<StreamController>;
    sendTyping(opts: { channel: string; threadTs?: string }): Promise<void>;
  };
  readonly threads: {
    getContext(opts: { channel: string; threadTs: string; limit?: number }): Promise<ThreadContext>;
    iterateReplies(opts: { channel: string; threadTs: string; pageSize?: number }): AsyncIterable<ThreadMessage>;
  };
  readonly files: {
    upload(opts: UploadOptions): Promise<FileInfo>;
    getInfo(fileId: string): Promise<FileInfo>;
    delete(fileId: string): Promise<void>;
    downloadStream(file: string | { urlPrivate: string; mimeType?: string; size?: number; id?: string }, opts?: { maxBytes?: number; signal?: AbortSignal }): Promise<ReadableStream<Uint8Array>>;
    downloadBuffer(file: string | { urlPrivate: string; mimeType?: string; size?: number; id?: string }, opts?: { maxBytes?: number; signal?: AbortSignal }): Promise<Buffer>;
  };
  readonly search: {
    messages(query: string, opts?: SearchOptions): Promise<SearchPage<SearchMessageHit>>;
    files(query: string, opts?: SearchOptions): Promise<SearchPage<FileInfo>>;
    channels(query: string, opts?: SearchOptions): Promise<SearchPage<ChannelRef>>;
    people(query: string, opts?: SearchOptions): Promise<SearchPage<UserRef>>;
    iterateMessages(query: string, opts?: Omit<SearchOptions, "page">): AsyncIterable<SearchMessageHit>;
    iterateFiles(query: string, opts?: Omit<SearchOptions, "page">): AsyncIterable<FileInfo>;
  };
  readonly reactions: {
    add(opts: { channel: string; ts: string; emoji: string }): Promise<void>;
    remove(opts: { channel: string; ts: string; emoji: string }): Promise<void>;
  };
  readonly users: {
    resolve(id: string): Promise<UserRef | null>;
    resolve(ids: string[]): Promise<Array<UserRef | null>>;
    getDnd(id: string): Promise<DndInfo>;
    getDnd(ids: string[]): Promise<Record<string, DndInfo>>;
  };
  readonly channels: {
    resolve(id: string): Promise<ChannelRef | null>;
    resolve(ids: string[]): Promise<Array<ChannelRef | null>>;
    getHistory(opts: { channel: string; limit?: number; oldest?: string; latest?: string; cursor?: string }): Promise<{ messages: HistoryMessage[]; hasMore: boolean; nextCursor?: string }>;
    iterateHistory(opts: { channel: string; oldest?: string; pageSize?: number }): AsyncIterable<HistoryMessage>;
  };
  readonly presence: { getStatus(userId: string): Promise<PresenceStatus> };

  constructor(
    private readonly policy: ScopePolicy,
    api: { chat: ChatApi; threads: ThreadsApi; files: FilesApi; search: SearchApi; reactions: ReactionsApi; users: UsersApi; channels: ChannelsApi; presence: PresenceApi },
    private readonly registry: SelfRegistry,
    private readonly logger: Logger,
  ) {
    const self = this;
    const suffix = policy.write?.disclosureSuffix ?? "";
    const withSuffix = (md: string | undefined) => (md !== undefined && suffix ? `${md}${suffix}` : md);

    this.chat = {
      async send(opts) {
        await self.checkWrite("chat.send", { channel: opts.channel, ...(opts.threadTs ? { threadTs: opts.threadTs } : {}) }, opts);
        const md = withSuffix(opts.markdown);
        return api.chat.send({ ...opts, ...(md !== undefined ? { markdown: md } : {}) });
      },
      async update(opts) {
        await self.checkWrite("chat.update", { channel: opts.channel, ts: opts.ts }, opts);
        const md = withSuffix(opts.markdown);
        return api.chat.update({ ...opts, ...(md !== undefined ? { markdown: md } : {}) });
      },
      async delete(opts) {
        await self.checkWrite("chat.delete", { channel: opts.channel, ts: opts.ts }, opts);
        return api.chat.delete(opts);
      },
      async createStream(opts) {
        await self.checkWrite("chat.createStream", { channel: opts.channel, ...(opts.threadTs ? { threadTs: opts.threadTs } : {}) }, opts);
        const stream = await api.chat.createStream(opts);
        if (!suffix) return stream;
        return {
          get ts() {
            return stream.ts;
          },
          get signal() {
            return stream.signal;
          },
          append: (c: string) => stream.append(c),
          abort: (r?: string) => stream.abort(r),
          finish: async () => {
            stream.append(suffix);
            return stream.finish();
          },
        };
      },
      async sendTyping(opts) {
        self.checkRead(opts.channel);
        return api.chat.sendTyping(opts);
      },
    };

    this.threads = {
      async getContext(opts) {
        self.checkRead(opts.channel);
        return api.threads.getContext(opts);
      },
      iterateReplies(opts) {
        self.checkRead(opts.channel);
        return api.threads.iterateReplies(opts);
      },
    };

    const download = async (file: string | { urlPrivate: string; mimeType?: string; size?: number; id?: string }, opts: { maxBytes?: number; signal?: AbortSignal } = {}) => {
      const limit = policy.read?.maxFileDownloadBytes;
      const id = typeof file === "string" ? (/^F[A-Z0-9]+$/.test(file) ? file : undefined) : file.id;
      if (policy.read?.channels) {
        if (!id) throw self.deny("files.download", "file must be identified by ID when read.channels is restricted");
        const info = await api.files.getInfo(id);
        if (!(info.sharedIn ?? []).some((c) => policy.read!.channels!.includes(c))) throw self.deny("files.download", "file is not shared in an allowed channel");
        if (limit !== undefined && info.size !== undefined && info.size > limit) throw self.deny("files.download", `file exceeds maxFileDownloadBytes (${limit})`);
      }
      if (typeof file !== "string" && limit !== undefined && file.size !== undefined && file.size > limit) {
        throw self.deny("files.download", `file exceeds maxFileDownloadBytes (${limit})`);
      }
      const callerMax = opts.maxBytes;
      const effective = limit === undefined ? callerMax : callerMax === undefined ? limit : Math.min(limit, callerMax);
      const scopeBinding = limit !== undefined && (callerMax === undefined || limit <= callerMax);
      try {
        const stream = await api.files.downloadStream(file, { ...opts, ...(effective !== undefined ? { maxBytes: effective } : {}) });
        if (!scopeBinding) return stream;
        // Policy limits win over resource limits: re-map mid-stream FILE_TOO_LARGE.
        const reader = stream.getReader();
        return new ReadableStream<Uint8Array>({
          async pull(controller) {
            try {
              const { done, value } = await reader.read();
              if (done) controller.close();
              else controller.enqueue(value);
            } catch (err) {
              controller.error(
                err instanceof GabrielError && err.code === "FILE_TOO_LARGE" ? self.deny("files.download", `file exceeds maxFileDownloadBytes (${limit})`) : err,
              );
            }
          },
          cancel: (reason) => reader.cancel(reason),
        });
      } catch (err) {
        if (scopeBinding && err instanceof GabrielError && err.code === "FILE_TOO_LARGE") throw self.deny("files.download", `file exceeds maxFileDownloadBytes (${limit})`);
        throw err;
      }
    };
    this.files = {
      async upload(opts) {
        await self.checkWrite("files.upload", { ...(opts.channel ? { channel: opts.channel } : {}), ...(opts.threadTs ? { threadTs: opts.threadTs } : {}) }, { filename: opts.filename, channel: opts.channel });
        const intro = withSuffix(opts.introMarkdown);
        return api.files.upload({ ...opts, ...(intro !== undefined ? { introMarkdown: intro } : {}) });
      },
      async getInfo(fileId) {
        const info = await api.files.getInfo(fileId);
        if (policy.read?.channels && !(info.sharedIn ?? []).some((c) => policy.read!.channels!.includes(c))) throw self.deny("files.getInfo", "file is not shared in an allowed channel");
        return info;
      },
      async delete(fileId) {
        await self.checkWrite("files.delete", {}, { fileId });
        return api.files.delete(fileId);
      },
      downloadStream: download,
      async downloadBuffer(file, opts = {}) {
        const stream = await download(file, opts);
        return Buffer.from(await new Response(stream).arrayBuffer());
      },
    };

    const allowedChannel = (id: string) => !policy.read?.channels || policy.read.channels.includes(id);
    this.search = {
      async messages(query, opts) {
        self.checkSearch();
        const page = await api.search.messages(query, opts);
        return { ...page, items: page.items.filter((h) => allowedChannel(h.channel.id)) };
      },
      async files(query, opts) {
        self.checkSearch();
        const page = await api.search.files(query, opts);
        return { ...page, items: page.items.filter((f) => !policy.read?.channels || (f.sharedIn ?? []).some(allowedChannel)) };
      },
      async channels(query, opts) {
        self.checkSearch();
        const page = await api.search.channels(query, opts);
        return { ...page, items: page.items.filter((c) => allowedChannel(c.id)) };
      },
      async people(query, opts) {
        self.checkSearch();
        return api.search.people(query, opts);
      },
      async *iterateMessages(query, opts) {
        self.checkSearch();
        for await (const hit of api.search.iterateMessages(query, opts)) if (allowedChannel(hit.channel.id)) yield hit;
      },
      async *iterateFiles(query, opts) {
        self.checkSearch();
        for await (const f of api.search.iterateFiles(query, opts)) if (!policy.read?.channels || (f.sharedIn ?? []).some(allowedChannel)) yield f;
      },
    };

    this.reactions = {
      async add(opts) {
        await self.checkWrite("reactions.add", { channel: opts.channel, ts: opts.ts }, opts);
        return api.reactions.add(opts);
      },
      async remove(opts) {
        await self.checkWrite("reactions.remove", { channel: opts.channel, ts: opts.ts }, opts);
        return api.reactions.remove(opts);
      },
    };

    this.users = api.users;
    this.channels = {
      resolve: api.channels.resolve.bind(api.channels) as ScopedClient["channels"]["resolve"],
      async getHistory(opts) {
        self.checkRead(opts.channel);
        return api.channels.getHistory(opts);
      },
      iterateHistory(opts) {
        self.checkRead(opts.channel);
        return api.channels.iterateHistory(opts);
      },
    };
    this.presence = { getStatus: (id) => api.presence.getStatus(id) };
  }

  /** The escape hatch bypasses every guard and is never exposed on a scoped view. */
  get core(): never {
    throw this.deny("core", "client.core is not available on a scoped client");
  }

  private deny(action: string, reason: string): GabrielError {
    const trigger = currentTrigger();
    this.logger.warn({ action, reason, triggerEventId: trigger?.eventId }, "scope policy violation");
    return violation(`${action}: ${reason}`);
  }

  private checkRead(channel: string): void {
    const allowed = this.policy.read?.channels;
    if (allowed && !allowed.includes(channel)) throw this.deny("read", `channel ${channel} is not in read.channels`);
  }

  private checkSearch(): void {
    if (this.policy.read?.allowSearch === false) throw this.deny("search", "search is disabled by policy");
  }

  private async checkWrite(action: ScopedWriteAction, target: Target, params: unknown): Promise<void> {
    const w = this.policy.write;
    if (w?.allowedActions && !w.allowedActions.includes(action)) throw this.deny(action, "action is not in write.allowedActions");
    if (w?.targetScope === "trigger_thread_only") {
      const trigger = currentTrigger();
      if (!trigger || !trigger.channel) throw this.deny(action, "trigger_thread_only writes must happen inside an event handler with a channel");
      if (target.channel !== trigger.channel) throw this.deny(action, "target channel is not the trigger channel");
      switch (action) {
        case "chat.send":
        case "chat.createStream":
        case "files.upload":
          if (!trigger.threadRootTs || target.threadTs !== trigger.threadRootTs) throw this.deny(action, "target thread is not the trigger thread");
          break;
        case "reactions.add":
        case "reactions.remove":
          if (target.ts !== trigger.ts && target.ts !== trigger.threadRootTs) throw this.deny(action, "reactions are limited to the trigger message");
          break;
        case "chat.update":
        case "chat.delete":
          if (!target.ts || !this.registry.isSelfSent(target.ts)) throw this.deny(action, "only messages this client sent can be changed");
          break;
        case "files.delete":
          throw this.deny(action, "files.delete has no thread target under trigger_thread_only");
      }
    }
    if (this.policy.approval) {
      let ok = false;
      try {
        ok = (await this.policy.approval(action, params)) === true;
      } catch (err) {
        this.logger.warn({ action, err: String(err) }, "approval hook threw; treating as refusal");
      }
      if (!ok) throw this.deny(action, "refused by approval hook");
    }
  }
}
