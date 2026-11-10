import { randomBytes, randomUUID } from "node:crypto";
import { Readable } from "node:stream";
import type { GabrielCore } from "../core/core.js";
import { GabrielError } from "../errors.js";
import { attributionUserIds, extractMentions, mentionReasonsOf, renderMessageMarkdown, toFileRefs } from "../events/normalize.js";
import { parseAttribution, splitAttribution, type MessageAttribution } from "../markdown/attribution.js";
import type { ChannelRef, DndInfo, FileRef, Mention, MentionReason, UserRef } from "../events/types.js";
import { toChannelRecord, toUserRecord, type Hydrator, type UserRecord } from "./hydrator.js";
import { assertChannelId, assertFileId, assertNonEmptyString, assertTs, assertUserId, compareTs, isTs, normalizeEmoji } from "../util/validate.js";

type Obj = Record<string, unknown>;
const s = (v: unknown): string | undefined => (typeof v === "string" && v.length > 0 ? v : undefined);
const o = (v: unknown): Obj => (v && typeof v === "object" ? (v as Obj) : {});

/** Highlight delimiters used by search results. */
const HIGHLIGHT = /[]/g;
export const stripHighlight = (text: string): string => text.replace(HIGHLIGHT, "");

function userRef(rec: UserRecord | null | undefined, id: string): UserRef {
  if (!rec) return { id };
  const ref: UserRef = { id };
  if (rec.name) ref.name = rec.name;
  if (rec.displayName) ref.displayName = rec.displayName;
  if (rec.handle) ref.handle = rec.handle;
  if (rec.isBot !== undefined) ref.isBot = rec.isBot;
  if (rec.isExternal !== undefined) ref.isExternal = rec.isExternal;
  return ref;
}

function channelRef(rec: { id: string; name?: string; isPrivate?: boolean; isIm?: boolean; isMpim?: boolean } | null | undefined, id: string): ChannelRef {
  if (!rec) return { id };
  const ref: ChannelRef = { id };
  if (rec.name) ref.name = rec.name;
  if (rec.isPrivate !== undefined) ref.isPrivate = rec.isPrivate;
  if (rec.isIm !== undefined) ref.isIm = rec.isIm;
  if (rec.isMpim !== undefined) ref.isMpim = rec.isMpim;
  return ref;
}

async function senderOf(h: Hydrator, msg: Obj): Promise<UserRef> {
  const bot = o(msg.bot_profile);
  const id = s(msg.user) ?? s(bot.user_id) ?? s(msg.bot_id) ?? "unknown";
  if (s(bot.name) && !s(msg.user)) return { id, name: s(bot.name)!, displayName: s(bot.name)!, isBot: true };
  if (!/^[UW]/.test(id)) return { id, isBot: true };
  const rec = await h.users.load(id).catch(() => undefined);
  return userRef(rec, id);
}

/** Loads every user / channel referenced by a list of messages, then renders Markdown. */
async function renderMessages(
  h: Hydrator,
  messages: Obj[],
): Promise<Array<{ msg: Obj; sender: UserRef; markdown: string; files: FileRef[]; mentions: Mention[]; mentionReasons: MentionReason[]; attribution?: MessageAttribution }>> {
  const ids = new Set<string>();
  for (const m of messages) {
    if (s(m.user)) ids.add(s(m.user)!);
    for (const mm of (s(m.text) ?? "").matchAll(/<@([UW][A-Z0-9]+)/g)) ids.add(mm[1]!);
    for (const id of attributionUserIds(m)) ids.add(id);
  }
  await Promise.allSettled([...ids].map((id) => h.users.load(id)));
  return Promise.all(
    messages.map(async (msg) => {
      const attribution = parseAttribution(msg.blocks, h.resolver);
      const mentions = extractMentions(splitAttribution(msg.blocks).rest, s(msg.text));
      for (const m of mentions) {
        const name = m.kind === "user" ? h.resolver.userName?.(m.id) : m.kind === "usergroup" ? h.resolver.usergroupHandle?.(m.id) : undefined;
        if (name) m.name = name;
      }
      return {
        msg,
        sender: await senderOf(h, msg),
        markdown: renderMessageMarkdown(msg, h.resolver),
        files: toFileRefs(msg.files),
        mentions,
        mentionReasons: mentionReasonsOf(mentions, { selfUserId: h.selfUserId, isMySubteam: (id) => h.subteamMembership(id) }),
        ...(attribution ? { attribution } : {}),
      };
    }),
  );
}

// ---------------------------------------------------------------------------------------------
// threads

export interface ThreadMessage {
  ts: string;
  sender: UserRef;
  markdown: string;
  /** Self-declared agent attribution line, removed from `markdown` (spec §9.1). Unverified. */
  attribution?: MessageAttribution;
  files: FileRef[];
  /** Users, user groups and broadcasts mentioned in the body. */
  mentions: Mention[];
  /** Why the message mentions me (same rules as `message.created.mentionReasons`). */
  mentionReasons: MentionReason[];
  isDeleted: boolean;
}

export interface ThreadContext {
  channel: ChannelRef;
  rootTs: string;
  messages: ThreadMessage[];
  participants: UserRef[];
  /** Readable transcript with resolved names, ready to use as LLM input. */
  formattedTranscript: string;
}

function formatTime(ts: string): string {
  const d = new Date(Number(ts.split(".")[0]) * 1000);
  return d.toISOString().replace("T", " ").replace(/\.\d+Z$/, "Z");
}

export function formatTranscript(channel: ChannelRef, rootTs: string, messages: ThreadMessage[]): string {
  const where = channel.isIm ? `direct message ${channel.name ?? channel.id}` : `#${channel.name ?? channel.id}`;
  const lines = [`Thread in ${where} (root ${rootTs}), ${messages.length} message(s):`, ""];
  for (const m of messages) {
    const who = m.sender.displayName ?? m.sender.name ?? m.sender.id;
    const handle = m.sender.handle ? ` (@${m.sender.handle})` : "";
    const body = m.isDeleted ? "_(message deleted)_" : m.markdown || (m.files.length ? "" : "_(empty)_");
    const files = m.files.length ? `\n  [files: ${m.files.map((f) => f.name).join(", ")}]` : "";
    const via = m.attribution ? ` [attribution: ${m.attribution.text.replace(/\s+/g, " ")}]` : "";
    lines.push(`[${formatTime(m.ts)}] ${who}${handle}${via}:`);
    lines.push(...body.split("\n").map((l) => `  ${l}`));
    if (files) lines.push(files);
    lines.push("");
  }
  return lines.join("\n").trimEnd();
}

export class ThreadsApi {
  constructor(
    private readonly core: GabrielCore,
    private readonly h: Hydrator,
    private readonly onWatch: (channel: string, threadTs: string) => void,
    private readonly onUnwatch: (channel: string, threadTs: string) => void,
  ) {}

  private async page(channel: string, ts: string, limit: number, cursor?: string, oldest?: string) {
    return this.core.callWebApi<{ messages?: Obj[]; has_more?: boolean; response_metadata?: { next_cursor?: string } }>(
      "conversations.replies",
      {
        channel,
        ts,
        inclusive: true,
        limit,
        include_tombstones: true,
        cached_latest_updates: "{}",
        ...(cursor ? { cursor } : {}),
        ...(oldest ? { oldest } : {}),
      },
      { reason: "history-api/fetchReplies" },
    );
  }

  /** Whole-thread context. `threadTs` may be the root or any reply; the root is located automatically. */
  async getContext(opts: { channel: string; threadTs: string; limit?: number }): Promise<ThreadContext> {
    const channel = assertChannelId(opts.channel);
    const ts = assertTs(opts.threadTs, "threadTs");
    const limit = Math.min(Math.max(1, opts.limit ?? 50), 1000);
    const raw: Obj[] = [];
    let cursor: string | undefined;
    do {
      const res = await this.page(channel, ts, Math.min(200, limit - raw.length), cursor);
      raw.push(...(res.messages ?? []));
      cursor = res.has_more ? s(res.response_metadata?.next_cursor) : undefined;
    } while (cursor && raw.length < limit);
    const first = raw[0];
    const rootTs = (first && (s(first.thread_ts) ?? s(first.ts))) ?? ts;
    for (const m of raw) if (s(m.ts)) this.h.recordThreadRoot(s(m.ts)!, s(m.thread_ts) ?? null);
    const rendered = await renderMessages(this.h, raw);
    const messages: ThreadMessage[] = rendered.map(({ msg, sender, markdown, files, mentions, mentionReasons, attribution }) => ({
      ts: s(msg.ts) ?? "",
      sender,
      markdown,
      ...(attribution ? { attribution } : {}),
      files,
      mentions,
      mentionReasons,
      isDeleted: msg.subtype === "tombstone" || msg.is_deleted === true,
    }));
    const participants = new Map<string, UserRef>();
    for (const m of messages) if (!participants.has(m.sender.id)) participants.set(m.sender.id, m.sender);
    const ch = await this.h.loadChannel(channel).catch(() => null);
    const chRef = channelRef(ch, channel);
    const imName = ch?.imUserId ? this.h.resolver.channelName?.(channel) : undefined;
    if (imName) chRef.name = imName;
    return { channel: chRef, rootTs, messages, participants: [...participants.values()], formattedTranscript: formatTranscript(chRef, rootTs, messages) };
  }

  async *iterateReplies(opts: { channel: string; threadTs: string; pageSize?: number; oldest?: string }): AsyncIterable<ThreadMessage> {
    const channel = assertChannelId(opts.channel);
    const ts = assertTs(opts.threadTs, "threadTs");
    const pageSize = Math.min(Math.max(1, opts.pageSize ?? 100), 200);
    let cursor: string | undefined;
    do {
      const res = await this.page(channel, ts, pageSize, cursor, opts.oldest);
      for (const r of await renderMessages(this.h, res.messages ?? [])) {
        yield {
          ts: s(r.msg.ts) ?? "",
          sender: r.sender,
          markdown: r.markdown,
          ...(r.attribution ? { attribution: r.attribution } : {}),
          files: r.files,
          mentions: r.mentions,
          mentionReasons: r.mentionReasons,
          isDeleted: r.msg.subtype === "tombstone",
        };
      }
      cursor = res.has_more ? s(res.response_metadata?.next_cursor) : undefined;
    } while (cursor);
  }

  async markRead(opts: { channel: string; threadTs: string; lastReadTs: string }): Promise<void> {
    await this.core.callWebApi(
      "subscriptions.thread.mark",
      { channel: assertChannelId(opts.channel), thread_ts: assertTs(opts.threadTs, "threadTs"), ts: assertTs(opts.lastReadTs, "lastReadTs"), read: 1 },
      { reason: "marking-thread" },
    );
  }

  async subscribe(opts: { channel: string; threadTs: string }): Promise<void> {
    const channel = assertChannelId(opts.channel);
    const threadTs = assertTs(opts.threadTs, "threadTs");
    await this.core.callWebApi("subscriptions.thread.add", { channel, thread_ts: threadTs }, { reason: "thread-subscribe" });
    this.onWatch(channel, threadTs);
  }

  async unsubscribe(opts: { channel: string; threadTs: string }): Promise<void> {
    const channel = assertChannelId(opts.channel);
    const threadTs = assertTs(opts.threadTs, "threadTs");
    await this.core.callWebApi("subscriptions.thread.remove", { channel, thread_ts: threadTs }, { reason: "thread-unsubscribe" });
    this.onUnwatch(channel, threadTs);
  }
}

// ---------------------------------------------------------------------------------------------
// files

export interface FileInfo extends FileRef {
  createdTs?: string;
  sharedIn?: string[];
}

export function toFileInfo(raw: Obj): FileInfo {
  const [ref] = toFileRefs([raw]);
  const info: FileInfo = ref ?? { id: String(raw.id ?? ""), name: "", mimeType: "application/octet-stream", urlPrivate: "" };
  if (typeof raw.created === "number") info.createdTs = String(raw.created);
  const shared = [raw.channels, raw.groups, raw.ims].flatMap((v) => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []));
  if (shared.length) info.sharedIn = shared;
  return info;
}

const NON_RETRYABLE_UPLOAD = new Set([
  "file_uploads_disabled",
  "file_uploads_except_images_disabled",
  "malware_detected",
  "files_upload_has_exceeded_limit",
  "blocked_file_type",
  "file_upload_size_restricted",
  "file_upload_quota_exceeded",
  "dlp_user_warning",
]);

export interface UploadOptions {
  filename: string;
  /** Buffer, web/Node readable stream, or text (a string is file content, never a path). */
  content: Buffer | Uint8Array | ReadableStream<Uint8Array> | Readable | string;
  /** Byte length; required to stream without buffering. */
  size?: number;
  title?: string;
  mimeType?: string;
  channel?: string;
  threadTs?: string;
  /** Plain-text message posted with the file. */
  introMarkdown?: string;
}

export class FilesApi {
  constructor(
    private readonly core: GabrielCore,
    private readonly h: Hydrator,
    /** Registers the share message's client_msg_id so its echo is recognized as this client's. */
    private readonly onClientMsgId: (id: string) => void = () => {},
  ) {}

  /** Three-step upload: files.getUploadURL → POST bytes → files.completeUpload (shares if channel given). */
  async upload(opts: UploadOptions): Promise<FileInfo> {
    const filename = assertNonEmptyString(opts.filename, "filename", 255);
    if (/[\\/\0]/.test(filename)) throw new GabrielError("INVALID_ARGUMENT", "filename must not contain path separators");
    const channel = opts.channel === undefined ? undefined : assertChannelId(opts.channel);
    if (opts.threadTs !== undefined) assertTs(opts.threadTs, "threadTs");
    let body: Buffer | ReadableStream<Uint8Array>;
    let length: number;
    if (typeof opts.content === "string") body = Buffer.from(opts.content, "utf8");
    else if (opts.content instanceof Uint8Array) body = Buffer.from(opts.content);
    else if (opts.content instanceof Readable) body = Readable.toWeb(opts.content) as ReadableStream<Uint8Array>;
    else body = opts.content;
    if (Buffer.isBuffer(body)) length = body.byteLength;
    else if (typeof opts.size === "number" && opts.size >= 0) length = opts.size;
    else {
      // Unknown length: the protocol needs it up front, so buffer the stream.
      body = Buffer.from(await new Response(body).arrayBuffer());
      length = body.byteLength;
    }

    const step1 = await this.core.callWebApi<{ file?: string; upload_url?: string }>(
      "files.getUploadURL",
      { filename, length },
      { reason: "upload-queue" },
    );
    const fileId = s(step1.file);
    const uploadUrl = s(step1.upload_url);
    if (!fileId || !uploadUrl) throw new GabrielError("SLACK_API_ERROR", "files.getUploadURL returned no upload URL");

    const attempts = Buffer.isBuffer(body) ? 3 : 1;
    for (let i = 0; ; i++) {
      const { stream, contentType } = multipartBody("file", filename, opts.mimeType ?? "application/octet-stream", body);
      try {
        const resp = await this.core.http.fetchAuthenticated(
          uploadUrl,
          { method: "POST", headers: { "Content-Type": contentType }, body: stream, duplex: "half", signal: AbortSignal.timeout(10 * 60_000) } as RequestInit & { signal: AbortSignal },
          { bearer: false },
        );
        await resp.body?.cancel().catch(() => {});
        if (resp.ok) break;
        const err = new GabrielError(resp.status >= 500 ? "SERVER_ERROR" : "SLACK_API_ERROR", `file upload failed (HTTP ${resp.status})`);
        if (resp.status < 500 || i + 1 >= attempts) throw err;
      } catch (err) {
        if (err instanceof GabrielError && err.code === "SLACK_API_ERROR") throw err;
        if (i + 1 >= attempts) throw err;
      }
    }

    const clientMsgId = randomUUID();
    this.onClientMsgId(clientMsgId);
    try {
      const intro = opts.introMarkdown
        ? [{ type: "rich_text", elements: [{ type: "rich_text_section", elements: [{ type: "text", text: opts.introMarkdown }] }] }]
        : undefined;
      const res = await this.core.callWebApi<{ files?: Obj[] }>(
        "files.completeUpload",
        {
          files: [{ id: fileId, title: opts.title ?? filename }],
          ...(channel ? { channel } : {}),
          ...(opts.threadTs ? { thread_ts: opts.threadTs } : {}),
          ...(intro ? { blocks: intro } : {}),
          client_msg_id: clientMsgId,
        },
        { reason: "upload-queue", retry: "none" },
      );
      const info = toFileInfo(res.files?.[0] ?? { id: fileId, name: filename });
      return info;
    } catch (err) {
      const raw = (err as GabrielError).rawSlackError;
      if (raw && NON_RETRYABLE_UPLOAD.has(raw)) throw new GabrielError("PERMISSION_DENIED", `upload rejected: ${raw}`, { rawSlackError: raw });
      throw err;
    }
  }

  async getInfo(fileId: string): Promise<FileInfo> {
    const res = await this.core.callWebApi<{ file?: Obj }>("files.info", { file: assertFileId(fileId) }, { reason: "fetching-a-file-object" });
    if (!res.file) throw new GabrielError("SLACK_API_ERROR", "files.info returned no file");
    return toFileInfo(res.file);
  }

  async delete(fileId: string): Promise<void> {
    await this.core.callWebApi("files.delete", { file: assertFileId(fileId) }, { reason: "delete-file" });
  }

  /** Streams a private file (cookie + bearer). Refuses sign-in pages; enforces `maxBytes`. */
  async downloadStream(fileOrUrl: string | { urlPrivate: string; mimeType?: string }, opts: { maxBytes?: number; signal?: AbortSignal } = {}): Promise<ReadableStream<Uint8Array>> {
    let url: string;
    let expectedMime: string | undefined;
    if (typeof fileOrUrl === "string") {
      if (/^F[A-Z0-9]+$/.test(fileOrUrl)) {
        const info = await this.getInfo(fileOrUrl);
        url = info.urlPrivate;
        expectedMime = info.mimeType;
      } else url = fileOrUrl;
    } else {
      url = fileOrUrl.urlPrivate;
      expectedMime = fileOrUrl.mimeType;
    }
    if (!url) throw new GabrielError("INVALID_ARGUMENT", "file has no private URL");
    const resp = await this.core.http.fetchAuthenticated(url, { method: "GET", ...(opts.signal ? { signal: opts.signal } : {}) });
    if (resp.status === 404) throw new GabrielError("SLACK_API_ERROR", "file not found", { rawSlackError: "file_not_found" });
    if (!resp.ok) throw new GabrielError(resp.status >= 500 ? "SERVER_ERROR" : "PERMISSION_DENIED", `file download failed (HTTP ${resp.status})`);
    const ctype = resp.headers.get("content-type") ?? "";
    if (ctype.includes("text/html") && expectedMime !== "text/html" && (resp.redirected || /signin|login/i.test(resp.url) || expectedMime !== undefined)) {
      await resp.body?.cancel().catch(() => {});
      throw new GabrielError("PERMISSION_DENIED", "download returned an HTML sign-in page; the session cookie / token pair is not accepted");
    }
    const max = opts.maxBytes;
    const declared = Number(resp.headers.get("content-length") ?? NaN);
    if (max !== undefined && Number.isFinite(declared) && declared > max) {
      await resp.body?.cancel().catch(() => {});
      throw new GabrielError("FILE_TOO_LARGE", `file is ${declared} bytes (> maxBytes ${max})`);
    }
    const source = resp.body ?? new ReadableStream<Uint8Array>({ start: (c) => c.close() });
    if (max === undefined) return source;
    let seen = 0;
    return source.pipeThrough(
      new TransformStream<Uint8Array, Uint8Array>({
        transform(chunk, controller) {
          seen += chunk.byteLength;
          if (seen > max) {
            controller.error(new GabrielError("FILE_TOO_LARGE", `download exceeded maxBytes ${max}`));
            return;
          }
          controller.enqueue(chunk);
        },
      }),
    );
  }

  async downloadBuffer(fileOrUrl: string | { urlPrivate: string; mimeType?: string }, opts: { maxBytes?: number; signal?: AbortSignal } = {}): Promise<Buffer> {
    const stream = await this.downloadStream(fileOrUrl, opts);
    const chunks: Uint8Array[] = [];
    const reader = stream.getReader();
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      chunks.push(value);
    }
    return Buffer.concat(chunks);
  }
}

function multipartBody(field: string, filename: string, mime: string, body: Buffer | ReadableStream<Uint8Array>): { stream: ReadableStream<Uint8Array>; contentType: string } {
  const boundary = `----gabriel${randomBytes(12).toString("hex")}`;
  const safeName = filename.replace(/["\r\n]/g, "_");
  const head = Buffer.from(
    `--${boundary}\r\nContent-Disposition: form-data; name="${field}"; filename="${safeName}"\r\nContent-Type: ${mime.replace(/[\r\n]/g, "")}\r\n\r\n`,
  );
  const tail = Buffer.from(`\r\n--${boundary}--\r\n`);
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      controller.enqueue(head);
      if (Buffer.isBuffer(body)) controller.enqueue(body);
      else {
        const reader = body.getReader();
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          controller.enqueue(value);
        }
      }
      controller.enqueue(tail);
      controller.close();
    },
  });
  return { stream, contentType: `multipart/form-data; boundary=${boundary}` };
}

// ---------------------------------------------------------------------------------------------
// search

export interface SearchOptions {
  count?: number;
  page?: number;
  sort?: "timestamp" | "score";
}

export interface SearchPage<T> {
  items: T[];
  page: number;
  hasMore: boolean;
  totalCount?: number;
}

export interface SearchMessageHit {
  ts: string;
  channel: ChannelRef;
  sender: UserRef;
  markdown: string;
  /** Self-declared agent attribution line, removed from `markdown` (spec §9.1). Unverified. */
  attribution?: MessageAttribution;
  files: FileRef[];
  mentions: Mention[];
  mentionReasons: MentionReason[];
  threadTs?: string;
  permalink?: string;
}

export class SearchApi {
  constructor(
    private readonly core: GabrielCore,
    private readonly h: Hydrator,
  ) {}

  private async module(module: string, query: string, opts: SearchOptions): Promise<{ items: Obj[]; page: number; hasMore: boolean; total?: number }> {
    const q = assertNonEmptyString(query, "query", 1000);
    const page = Math.max(1, Math.floor(opts.page ?? 1));
    const count = Math.min(100, Math.max(1, Math.floor(opts.count ?? 20)));
    const res = await this.core.callWebApi<{ items?: Obj[]; pagination?: { page?: number; page_count?: number; total_count?: number } }>(
      `search.modules.${module}`,
      {
        module,
        query: q,
        page,
        count,
        sort: opts.sort ?? "timestamp",
        sort_dir: "desc",
        extracts: 1,
        highlight: 1,
        max_extract_len: 200,
        extra_message_data: 1,
        no_user_profile: 1,
        include_files_shares: 1,
        search_context: `desktop_${module}_tab`,
      },
      { reason: "search-api/fetchSearchModule" },
    );
    const p = res.pagination ?? {};
    const items = res.items ?? [];
    const hasMore = typeof p.page_count === "number" ? page < p.page_count : items.length >= count;
    return { items, page, hasMore, ...(typeof p.total_count === "number" ? { total: p.total_count } : {}) };
  }

  async messages(query: string, opts: SearchOptions = {}): Promise<SearchPage<SearchMessageHit>> {
    const r = await this.module("messages", query, opts);
    const flat: Array<{ msg: Obj; channel: Obj }> = [];
    for (const item of r.items) {
      const channel = o(item.channel);
      const msgs = Array.isArray(item.messages) ? (item.messages as Obj[]) : [item];
      for (const m of msgs) flat.push({ msg: m, channel: Object.keys(channel).length ? channel : o(m.channel) });
    }
    const rendered = await renderMessages(
      this.h,
      flat.map((f) => ({ ...f.msg, text: stripHighlight(s(f.msg.text) ?? "") })),
    );
    const hits: SearchMessageHit[] = [];
    rendered.forEach((r2, i) => {
      const ch = flat[i]!.channel;
      const ts = s(r2.msg.ts);
      if (!ts) return;
      const chId = s(ch.id) ?? "";
      const hit: SearchMessageHit = {
        ts,
        channel: {
          id: chId,
          ...(s(ch.name) ? { name: stripHighlight(s(ch.name)!) } : {}),
          ...(typeof ch.is_private === "boolean" ? { isPrivate: ch.is_private } : {}),
          ...(typeof ch.is_im === "boolean" ? { isIm: ch.is_im } : {}),
          ...(typeof ch.is_mpim === "boolean" ? { isMpim: ch.is_mpim } : {}),
        },
        sender: r2.sender,
        markdown: stripHighlight(r2.markdown),
        ...(r2.attribution ? { attribution: r2.attribution } : {}),
        files: r2.files,
        mentions: r2.mentions,
        mentionReasons: r2.mentionReasons,
      };
      const thread = s(r2.msg.thread_ts);
      if (thread) hit.threadTs = thread;
      const permalink = s(r2.msg.permalink);
      if (permalink) hit.permalink = permalink;
      hits.push(hit);
    });
    return { items: hits, page: r.page, hasMore: r.hasMore, ...(r.total !== undefined ? { totalCount: r.total } : {}) };
  }

  async files(query: string, opts: SearchOptions = {}): Promise<SearchPage<FileInfo>> {
    const r = await this.module("files", query, opts);
    const items = r.items.map((it) => {
      const info = toFileInfo(it);
      info.name = stripHighlight(info.name);
      if (info.title) info.title = stripHighlight(info.title);
      return info;
    });
    return { items, page: r.page, hasMore: r.hasMore, ...(r.total !== undefined ? { totalCount: r.total } : {}) };
  }

  async channels(query: string, opts: SearchOptions = {}): Promise<SearchPage<ChannelRef>> {
    const r = await this.module("channels", query, opts);
    const items = r.items.map((c) => {
      const ref: ChannelRef = { id: s(c.id) ?? "" };
      if (s(c.name)) ref.name = stripHighlight(s(c.name)!);
      if (typeof c.is_private === "boolean") ref.isPrivate = c.is_private;
      if (typeof c.is_mpim === "boolean") ref.isMpim = c.is_mpim;
      return ref;
    });
    return { items, page: r.page, hasMore: r.hasMore, ...(r.total !== undefined ? { totalCount: r.total } : {}) };
  }

  async people(query: string, opts: SearchOptions = {}): Promise<SearchPage<UserRef>> {
    const r = await this.module("people", query, opts);
    const teamId = this.core.session.teamId;
    const items = r.items.map((u) => userRef(toUserRecord({ ...u, name: stripHighlight(s(u.name) ?? "") || undefined }, teamId), s(u.id) ?? ""));
    return { items, page: r.page, hasMore: r.hasMore, ...(r.total !== undefined ? { totalCount: r.total } : {}) };
  }

  /** Paged iterator de-duplicated by ts. Search pagination is not a snapshot: results may shift between pages. */
  async *iterateMessages(query: string, opts: Omit<SearchOptions, "page"> & { maxPages?: number } = {}): AsyncIterable<SearchMessageHit> {
    const seen = new Set<string>();
    const maxPages = opts.maxPages ?? 50;
    for (let page = 1; page <= maxPages; page++) {
      const res = await this.messages(query, { ...opts, page });
      for (const hit of res.items) {
        const key = `${hit.channel.id}:${hit.ts}`;
        if (seen.has(key)) continue;
        seen.add(key);
        yield hit;
      }
      if (!res.hasMore) return;
    }
  }

  async *iterateFiles(query: string, opts: Omit<SearchOptions, "page"> & { maxPages?: number } = {}): AsyncIterable<FileInfo> {
    const seen = new Set<string>();
    const maxPages = opts.maxPages ?? 50;
    for (let page = 1; page <= maxPages; page++) {
      const res = await this.files(query, { ...opts, page });
      for (const f of res.items) {
        if (seen.has(f.id)) continue;
        seen.add(f.id);
        yield f;
      }
      if (!res.hasMore) return;
    }
  }
}

// ---------------------------------------------------------------------------------------------
// reactions

export class ReactionsApi {
  constructor(private readonly core: GabrielCore) {}

  /** Idempotent: `already_reacted` is treated as success. */
  async add(opts: { channel: string; ts: string; emoji: string }): Promise<void> {
    await this.call("reactions.add", opts, "already_reacted");
  }

  /** Idempotent: `no_reaction` is treated as success. */
  async remove(opts: { channel: string; ts: string; emoji: string }): Promise<void> {
    await this.call("reactions.remove", opts, "no_reaction");
  }

  private async call(method: string, opts: { channel: string; ts: string; emoji: string }, benign: string): Promise<void> {
    try {
      await this.core.callWebApi(
        method,
        { channel: assertChannelId(opts.channel), timestamp: assertTs(opts.ts), name: normalizeEmoji(opts.emoji) },
        { reason: "changeReactionFromUserAction" },
      );
    } catch (err) {
      if ((err as GabrielError).rawSlackError === benign) return;
      throw err;
    }
  }
}

// ---------------------------------------------------------------------------------------------
// users & channels

export class UsersApi {
  constructor(private readonly h: Hydrator) {}

  resolve(id: string): Promise<UserRef | null>;
  resolve(ids: string[]): Promise<Array<UserRef | null>>;
  async resolve(idOrIds: string | string[]): Promise<UserRef | null | Array<UserRef | null>> {
    if (Array.isArray(idOrIds)) {
      const ids = idOrIds.map((id) => assertUserId(id));
      const recs = await this.h.users.loadMany(ids);
      return recs.map((r, i) => (r ? userRef(r, ids[i]!) : null));
    }
    const id = assertUserId(idOrIds);
    const rec = await this.h.users.load(id);
    return rec ? userRef(rec, id) : null;
  }

  getDnd(id: string): Promise<DndInfo>;
  getDnd(ids: string[]): Promise<Record<string, DndInfo>>;
  async getDnd(idOrIds: string | string[]): Promise<DndInfo | Record<string, DndInfo>> {
    if (Array.isArray(idOrIds)) return this.h.fetchDnd(idOrIds.map((id) => assertUserId(id)));
    const id = assertUserId(idOrIds);
    const map = await this.h.fetchDnd([id]);
    return map[id] ?? { enabled: false, nextStartTs: 0, nextEndTs: 0 };
  }
}

export interface HistoryMessage {
  ts: string;
  sender: UserRef;
  markdown: string;
  /** Self-declared agent attribution line, removed from `markdown` (spec §9.1). Unverified. */
  attribution?: MessageAttribution;
  files: FileRef[];
  /** Users, user groups and broadcasts mentioned in the body. */
  mentions: Mention[];
  /** Why the message mentions me (same rules as `message.created.mentionReasons`). */
  mentionReasons: MentionReason[];
  thread: { rootTs: string; replyCount: number; latestReplyTs?: string } | null;
  /** Raw message subtype (`channel_join`, `bot_message`, `thread_broadcast`, …) if any. */
  subtype?: string;
  isDeleted: boolean;
}

/** A 1:1 direct message or a group direct message I am in. */
export interface DirectConversation {
  /** `isIm` / `isMpim` are set; a 1:1 DM is named `@peer` when the peer resolves. */
  channel: ChannelRef;
  /** Timestamp of the newest message, when Slack reports one. */
  latestTs?: string;
  /** Whether the conversation is open in my sidebar. */
  isOpen?: boolean;
  /** The other person in a 1:1 DM. */
  peer?: UserRef;
}

type Rendered = Awaited<ReturnType<typeof renderMessages>>[number];

export class ChannelsApi {
  constructor(
    private readonly core: GabrielCore,
    private readonly h: Hydrator,
  ) {}

  resolve(id: string): Promise<ChannelRef | null>;
  resolve(ids: string[]): Promise<Array<ChannelRef | null>>;
  async resolve(idOrIds: string | string[]): Promise<ChannelRef | null | Array<ChannelRef | null>> {
    const one = async (id: string) => {
      const rec = await this.h.loadChannel(assertChannelId(id));
      if (!rec) return null;
      const ref = channelRef(rec, id);
      if (rec.isIm && rec.imUserId) {
        await this.h.users.load(rec.imUserId).catch(() => null);
        const name = this.h.resolver.channelName?.(id);
        if (name) ref.name = name;
      }
      return ref;
    };
    return Array.isArray(idOrIds) ? Promise.all(idOrIds.map(one)) : one(idOrIds);
  }

  /** Channel history (main timeline + broadcast replies; plain thread replies are not included). */
  async getHistory(opts: { channel: string; limit?: number; oldest?: string; latest?: string; cursor?: string; inclusive?: boolean }): Promise<{
    messages: HistoryMessage[];
    hasMore: boolean;
    nextCursor?: string;
  }> {
    const res = await this.rawHistory(opts);
    const rendered = await renderMessages(this.h, res.messages);
    const messages = rendered.map(toHistoryMessage);
    return { messages, hasMore: res.hasMore, ...(res.nextCursor ? { nextCursor: res.nextCursor } : {}) };
  }

  /**
   * One page of my direct conversations (1:1 and group), as the DMs view lists them.
   * Read-only: does not open, close or mark any conversation.
   */
  async listDirect(opts: { limit?: number; cursor?: string; includeClosed?: boolean } = {}): Promise<{ items: DirectConversation[]; nextCursor?: string }> {
    const res = await this.core.callWebApi<{ dms?: Obj[]; ims?: Obj[]; mpims?: Obj[]; response_metadata?: { next_cursor?: string } }>(
      "client.dms",
      {
        count: Math.min(250, Math.max(1, Math.floor(opts.limit ?? 50))),
        include_closed: opts.includeClosed ?? true,
        include_channel: true,
        exclude_bots: false,
        ...(opts.cursor ? { cursor: assertNonEmptyString(opts.cursor, "cursor", 2000) } : {}),
      },
      { reason: "dms-tab-populate" },
    );
    const seen = new Set<string>();
    const rows: Array<{ rec: ReturnType<typeof toChannelRecord>; latestTs?: string; isOpen?: boolean }> = [];
    for (const entry of [...(res.dms ?? []), ...(res.ims ?? []), ...(res.mpims ?? [])]) {
      const raw = o(entry.channel);
      const id = s(raw.id) ?? s(entry.id);
      if (!id || seen.has(id)) continue;
      seen.add(id);
      const rec = toChannelRecord({ ...raw, id });
      if (rec.isIm === undefined && rec.isMpim === undefined) rec.isIm = id.startsWith("D");
      this.h.primeChannel(rec);
      const latest = s(entry.latest) ?? s(o(entry.message).ts);
      rows.push({ rec, ...(latest && isTs(latest) ? { latestTs: latest } : {}), ...(typeof raw.is_open === "boolean" ? { isOpen: raw.is_open } : {}) });
    }
    const peers = [...new Set(rows.flatMap((r) => (r.rec.imUserId ? [r.rec.imUserId] : [])))];
    await Promise.allSettled(peers.map((id) => this.h.users.load(id)));
    const items = rows.map(({ rec, latestTs, isOpen }): DirectConversation => {
      const channel = channelRef(rec, rec.id);
      const name = rec.imUserId ? this.h.resolver.channelName?.(rec.id) : undefined;
      if (name) channel.name = name;
      return {
        channel,
        ...(latestTs ? { latestTs } : {}),
        ...(isOpen !== undefined ? { isOpen } : {}),
        ...(rec.imUserId ? { peer: userRef(this.h.users.peek(rec.imUserId), rec.imUserId) } : {}),
      };
    });
    const nextCursor = s(res.response_metadata?.next_cursor);
    return { items, ...(nextCursor ? { nextCursor } : {}) };
  }

  /** Iterates my direct conversations. `activeSince` skips those whose newest message is older (or unknown). */
  async *iterateDirect(opts: { activeSince?: string; includeClosed?: boolean; pageSize?: number; maxPages?: number } = {}): AsyncIterable<DirectConversation> {
    if (opts.activeSince !== undefined) assertTs(opts.activeSince, "activeSince");
    const seen = new Set<string>();
    let cursor: string | undefined;
    for (let page = 0; page < (opts.maxPages ?? 50); page++) {
      const res = await this.listDirect({ ...(opts.pageSize ? { limit: opts.pageSize } : {}), ...(opts.includeClosed !== undefined ? { includeClosed: opts.includeClosed } : {}), ...(cursor ? { cursor } : {}) });
      for (const item of res.items) {
        if (seen.has(item.channel.id)) continue;
        seen.add(item.channel.id);
        if (opts.activeSince !== undefined && (!item.latestTs || compareTs(item.latestTs, opts.activeSince) < 0)) continue;
        yield item;
      }
      if (!res.nextCursor || res.nextCursor === cursor) return;
      cursor = res.nextCursor;
    }
  }

  /**
   * Fetches specific messages of one conversation by timestamp (top-level messages and thread
   * replies alike). Messages that no longer exist are left out; order follows `timestamps`.
   */
  async getMessages(opts: { channel: string; timestamps: string[] }): Promise<HistoryMessage[]> {
    const channel = assertChannelId(opts.channel);
    const byChannel = await fetchMessages(this.core, this.h, new Map([[channel, opts.timestamps.map((ts) => assertTs(ts, "timestamps"))]]));
    const found = byChannel.get(channel) ?? new Map<string, HistoryMessage>();
    return [...new Set(opts.timestamps)].flatMap((ts) => (found.has(ts) ? [found.get(ts)!] : []));
  }

  /** Raw `conversations.history` page (used by gap recovery). */
  async rawHistory(opts: { channel: string; limit?: number; oldest?: string; latest?: string; cursor?: string; inclusive?: boolean }): Promise<{
    messages: Obj[];
    hasMore: boolean;
    nextCursor?: string;
  }> {
    const channel = assertChannelId(opts.channel);
    if (opts.oldest !== undefined) assertTs(opts.oldest, "oldest");
    if (opts.latest !== undefined) assertTs(opts.latest, "latest");
    const res = await this.core.callWebApi<{ messages?: Obj[]; has_more?: boolean; response_metadata?: { next_cursor?: string } }>(
      "conversations.history",
      {
        channel,
        limit: Math.min(200, Math.max(1, opts.limit ?? 50)),
        inclusive: opts.inclusive ?? false,
        include_pin_count: false,
        no_user_profile: true,
        include_free_team_extra_messages: true,
        include_date_joined: false,
        cached_latest_updates: "{}",
        ...(opts.oldest ? { oldest: opts.oldest } : {}),
        ...(opts.latest ? { latest: opts.latest } : {}),
        ...(opts.cursor ? { cursor: opts.cursor } : {}),
      },
      { reason: "history-api/fetchHistory" },
    );
    const nextCursor = s(res.response_metadata?.next_cursor);
    return { messages: res.messages ?? [], hasMore: res.has_more === true, ...(nextCursor ? { nextCursor } : {}) };
  }

  /** Iterates history newest → oldest. The upper bound is pinned on the first page. */
  async *iterateHistory(opts: { channel: string; oldest?: string; pageSize?: number }): AsyncIterable<HistoryMessage> {
    let cursor: string | undefined;
    let latest: string | undefined;
    do {
      const page = await this.rawHistory({
        channel: opts.channel,
        limit: opts.pageSize ?? 100,
        ...(opts.oldest ? { oldest: opts.oldest } : {}),
        ...(latest ? { latest } : {}),
        ...(cursor ? { cursor } : {}),
      });
      if (latest === undefined) {
        const first = page.messages[0];
        latest = first && isTs(first.ts) ? first.ts : undefined;
        if (latest) cursor = page.nextCursor;
      } else cursor = page.nextCursor;
      for (const r of await renderMessages(this.h, page.messages)) yield toHistoryMessage(r);
      if (!page.hasMore) return;
    } while (cursor);
  }
}

function toHistoryMessage({ msg, sender, markdown, files, mentions, mentionReasons, attribution }: Rendered): HistoryMessage {
  const replyCount = typeof msg.reply_count === "number" ? msg.reply_count : 0;
  const threadTs = s(msg.thread_ts);
  const latest = s(msg.latest_reply);
  const subtype = s(msg.subtype);
  return {
    ts: s(msg.ts) ?? "",
    sender,
    markdown,
    ...(attribution ? { attribution } : {}),
    files,
    mentions,
    mentionReasons,
    thread: threadTs ? { rootTs: threadTs, replyCount, ...(latest ? { latestReplyTs: latest } : {}) } : null,
    ...(subtype ? { subtype } : {}),
    isDeleted: subtype === "tombstone" || msg.is_deleted === true,
  };
}

const MESSAGES_LIST_MAX_MESSAGES = 50;
/** `messages.list` rejects more channels per call with `too_many_channels`. */
const MESSAGES_LIST_MAX_CHANNELS = 20;

/** `messages.list`: full messages for (channel, ts) pairs, keyed by channel then ts. */
async function fetchMessages(core: GabrielCore, h: Hydrator, wanted: Map<string, string[]>): Promise<Map<string, Map<string, HistoryMessage>>> {
  const batches: Array<Map<string, string[]>> = [];
  let size = 0;
  for (const [channel, tss] of wanted) {
    for (const ts of new Set(tss)) {
      let batch = batches[batches.length - 1];
      if (!batch || size >= MESSAGES_LIST_MAX_MESSAGES || (!batch.has(channel) && batch.size >= MESSAGES_LIST_MAX_CHANNELS)) {
        batches.push((batch = new Map()));
        size = 0;
      }
      batch.set(channel, [...(batch.get(channel) ?? []), ts]);
      size++;
    }
  }
  const out = new Map<string, Map<string, HistoryMessage>>();
  for (const ids of batches) {
    const res = await core.callWebApi<{ messages_data?: Record<string, { messages?: Obj[] }> }>(
      "messages.list",
      { message_ids: [...ids].map(([channel, timestamps]) => ({ channel, timestamps })), org_wide_aware: true, cached_latest_updates: "{}" },
      { reason: "messages-ufm" },
    );
    for (const [channel, data] of Object.entries(res.messages_data ?? {})) {
      const raw = (data.messages ?? []).filter((m) => isTs(m.ts));
      for (const m of raw) h.recordThreadRoot(m.ts as string, s(m.thread_ts) ?? null);
      const bucket = out.get(channel) ?? new Map<string, HistoryMessage>();
      for (const r of await renderMessages(h, raw)) bucket.set(r.msg.ts as string, toHistoryMessage(r));
      out.set(channel, bucket);
    }
  }
  return out;
}

// ---------------------------------------------------------------------------------------------
// activity

/** Activity feed entry kinds. Mentions: `at_user`, `at_user_group`, `at_channel`, `at_everyone`, `keyword`. */
export type ActivityType = "at_user" | "at_user_group" | "at_channel" | "at_everyone" | "keyword" | "dm" | "thread_v2" | "message_reaction" | (string & {});

export interface ActivityItem {
  /** Stable key of the feed entry. */
  key: string;
  type: ActivityType;
  /** When the entry entered the feed. */
  feedTs: string;
  isUnread: boolean;
  /** Conversation of the message the entry points at, when it points at one. */
  channel?: ChannelRef;
  ts?: string;
  threadTs?: string;
  /** The message itself (absent when it was deleted, is not readable, or `includeMessages` is false). */
  message?: HistoryMessage;
}

function activityTarget(item: Obj): { channel: string; ts: string; threadTs?: string } | undefined {
  const payload = o(o(item.bundle_info).payload);
  const thread = o(payload.thread_entry);
  if (s(thread.channel_id) && isTs(thread.latest_ts)) {
    return { channel: s(thread.channel_id)!, ts: thread.latest_ts, ...(isTs(thread.thread_ts) ? { threadTs: thread.thread_ts } : {}) };
  }
  for (const m of [o(item.message), o(payload.message), o(o(payload.dm_entry).latest_message)]) {
    if (s(m.channel) && isTs(m.ts)) return { channel: s(m.channel)!, ts: m.ts, ...(isTs(m.thread_ts) ? { threadTs: m.thread_ts } : {}) };
  }
  return undefined;
}

/** The Activity feed (mentions, DMs, thread replies, reactions). Reading it does not mark anything read. */
export class ActivityApi {
  constructor(
    private readonly core: GabrielCore,
    private readonly h: Hydrator,
  ) {}

  /** One page of the feed, newest first. `types` selects the entry kinds, e.g. `["at_user"]` for messages that name me. */
  async list(opts: { types: ActivityType[]; limit?: number; cursor?: string; unreadOnly?: boolean; includeMessages?: boolean }): Promise<{ items: ActivityItem[]; nextCursor?: string }> {
    if (!Array.isArray(opts.types) || opts.types.length === 0) throw new GabrielError("INVALID_ARGUMENT", "types must list at least one activity type");
    for (const t of opts.types) if (!/^[a-z][a-z0-9_]{0,60}$/.test(t)) throw new GabrielError("INVALID_ARGUMENT", "invalid activity type");
    const res = await this.core.callWebApi<{ items?: Obj[]; response_metadata?: { next_cursor?: string } }>(
      "activity.feed",
      {
        limit: Math.min(50, Math.max(1, Math.floor(opts.limit ?? 20))),
        types: [...new Set(opts.types)].join(","),
        mode: opts.unreadOnly ? "chrono_unreads" : "chrono_reads_and_unreads",
        ...(opts.unreadOnly ? { unread_only: true } : {}),
        ...(opts.cursor ? { cursor: assertNonEmptyString(opts.cursor, "cursor", 2000) } : {}),
      },
      { reason: "fetchActivityFeed" },
    );
    const entries = (res.items ?? []).flatMap((raw) => {
      const item = o(raw.item);
      const type = s(item.type);
      const feedTs = s(raw.feed_ts);
      if (!type || !feedTs) return [];
      return [{ key: s(raw.key) ?? `${type}:${feedTs}`, type, feedTs, isUnread: raw.is_unread === true, target: activityTarget(item) }];
    });
    const wanted = new Map<string, string[]>();
    for (const e of entries) if (e.target) wanted.set(e.target.channel, [...(wanted.get(e.target.channel) ?? []), e.target.ts]);
    const [messages] = await Promise.all([
      opts.includeMessages === false ? new Map<string, Map<string, HistoryMessage>>() : fetchMessages(this.core, this.h, wanted),
      Promise.allSettled([...wanted.keys()].map((id) => this.h.loadChannel(id))),
    ]);
    const peers = [...wanted.keys()].flatMap((id) => (this.h.peekChannel(id)?.imUserId ? [this.h.peekChannel(id)!.imUserId!] : []));
    await Promise.allSettled(peers.map((id) => this.h.users.load(id)));
    const items = entries.map(({ target, ...base }): ActivityItem => {
      if (!target) return base;
      const rec = this.h.peekChannel(target.channel);
      const channel = channelRef(rec, target.channel);
      const name = rec?.imUserId ? this.h.resolver.channelName?.(target.channel) : undefined;
      if (name) channel.name = name;
      const message = messages.get(target.channel)?.get(target.ts);
      const threadTs = target.threadTs ?? message?.thread?.rootTs;
      return { ...base, channel, ts: target.ts, ...(threadTs ? { threadTs } : {}), ...(message ? { message } : {}) };
    });
    const nextCursor = s(res.response_metadata?.next_cursor);
    return { items, ...(nextCursor ? { nextCursor } : {}) };
  }

  /** Iterates the feed newest → oldest, de-duplicated by key; stops at the first entry older than `since`. */
  async *iterate(opts: { types: ActivityType[]; since?: string; unreadOnly?: boolean; pageSize?: number; maxPages?: number; includeMessages?: boolean }): AsyncIterable<ActivityItem> {
    if (opts.since !== undefined) assertTs(opts.since, "since");
    const seen = new Set<string>();
    let cursor: string | undefined;
    for (let page = 0; page < (opts.maxPages ?? 100); page++) {
      const res = await this.list({
        types: opts.types,
        ...(opts.pageSize ? { limit: opts.pageSize } : {}),
        ...(opts.unreadOnly ? { unreadOnly: true } : {}),
        ...(opts.includeMessages === false ? { includeMessages: false } : {}),
        ...(cursor ? { cursor } : {}),
      });
      for (const item of res.items) {
        if (opts.since !== undefined && compareTs(item.feedTs, opts.since) < 0) return;
        if (seen.has(item.key)) continue;
        seen.add(item.key);
        yield item;
      }
      if (!res.nextCursor || res.nextCursor === cursor) return;
      cursor = res.nextCursor;
    }
  }
}
