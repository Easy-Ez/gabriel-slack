import { EventEmitter } from "node:events";
import type { RawWsFrame, WebSocketLike } from "../src/core/types.js";
import { GabrielClient, type GabrielClientOptions } from "../src/client/client.js";

export const TEAM = "T0123456789";
export const SELF = "U0SELF00001";
export const TOKEN_1 = "xoxc-test-token-1";
export const COOKIE = "xoxd-test-cookie-value";

export const ENDPOINTS = {
  authUrl: "https://app.slack.test/auth?app=client&response_type=json",
  publicApiBase: "https://slack.test/api/",
  edgeApiBase: "https://edgeapi.slack.test/cache/",
  wsBase: "wss://ws.slack.test/",
};

export class FakeSocket extends EventEmitter implements WebSocketLike {
  readyState = 0;
  readonly sent: RawWsFrame[] = [];
  closedWith: number | undefined;

  constructor(readonly url: string, readonly headers: Record<string, string>) {
    super();
  }

  send(data: string): void {
    this.sent.push(JSON.parse(data) as RawWsFrame);
    this.emit("sent", this.sent[this.sent.length - 1]);
  }

  close(code?: number): void {
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.closedWith = code;
    setImmediate(() => this.emit("close", code ?? 1000, Buffer.from("")));
  }

  // server-side helpers
  open(): void {
    this.readyState = 1;
    this.emit("open");
  }

  deliver(frame: RawWsFrame): void {
    this.emit("message", JSON.stringify(frame));
  }

  serverClose(code = 1006): void {
    this.readyState = 3;
    this.emit("close", code, Buffer.from(""));
  }

  sentOfType(type: string): RawWsFrame[] {
    return this.sent.filter((f) => f.type === type);
  }

  override removeAllListeners(): this {
    super.removeAllListeners();
    return this;
  }
}

type ApiHandler = (fields: Record<string, string>, call: number) => unknown | Promise<unknown>;

export interface ApiCall {
  method: string;
  fields: Record<string, string>;
}

/** In-memory Slack: Web API, EdgeAPI, cookie exchange and WebSocket gateway. */
export class FakeSlack {
  readonly sockets: FakeSocket[] = [];
  readonly calls: ApiCall[] = [];
  readonly edgeCalls: Array<{ endpoint: string; body: Record<string, unknown> }> = [];
  authCalls = 0;
  token = TOKEN_1;
  authAlive = true;
  autoHello = true;
  helloFast: boolean | ((n: number) => boolean) = false;
  users = new Map<string, Record<string, unknown>>();
  channels = new Map<string, Record<string, unknown>>();
  private readonly api = new Map<string, ApiHandler>();
  private readonly apiCounts = new Map<string, number>();
  fileBodies = new Map<string, { body: Uint8Array; contentType: string }>();
  postedTs = 1_800_000_000;

  constructor() {
    this.channels.set("C0GENERAL01", { id: "C0GENERAL01", name: "general", is_channel: true, is_private: false });
    this.users.set(SELF, { id: SELF, name: "me", team_id: TEAM, profile: { display_name: "Me" } });
    this.users.set("U0ALICE0001", { id: "U0ALICE0001", name: "alice", team_id: TEAM, profile: { display_name: "Alice", status_text: "", status_emoji: "" } });
    this.users.set("U0BOB000001", { id: "U0BOB000001", name: "bob", team_id: TEAM, profile: { display_name: "Bob" } });
    this.on("usergroups.list", () => ({ ok: true, usergroups: [{ id: "S0ONCALL001", handle: "oncall", users: [SELF, "U0BOB000001"] }] }));
    this.on("chat.postMessage", (f) => {
      const ts = `${++this.postedTs}.000100`;
      return { ok: true, channel: f.channel, ts, message: { ts, text: f.markdown_text ?? "", user: SELF } };
    });
    this.on("chat.update", (f) => ({ ok: true, channel: f.channel, ts: f.ts }));
    this.on("client.counts", () => ({ ok: true, channels: [], mpims: [], ims: [] }));
    this.on("conversations.replies", () => ({ ok: true, messages: [], has_more: false }));
    this.on("reactions.add", () => ({ ok: true }));
  }

  on(method: string, handler: ApiHandler): this {
    this.api.set(method, handler);
    return this;
  }

  callsTo(method: string): ApiCall[] {
    return this.calls.filter((c) => c.method === method);
  }

  get socket(): FakeSocket {
    const s = this.sockets[this.sockets.length - 1];
    if (!s) throw new Error("no socket yet");
    return s;
  }

  readonly fetch = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url);
    const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
      new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", ...headers } });

    if (url.hostname === "app.slack.test") {
      this.authCalls++;
      await new Promise((r) => setTimeout(r, 5));
      if (!this.authAlive) return new Response("", { status: 302, headers: { location: "https://slack.test/signin" } });
      return json({
        teams: { [TEAM]: { id: TEAM, name: "Test", domain: "test", url: "https://test.slack.com/", token: this.token, user_id: SELF } },
        lc: "1",
      });
    }
    if (url.hostname === "edgeapi.slack.test") {
      const endpoint = url.pathname.split("/").slice(3).join("/");
      const body = JSON.parse(String(init?.body ?? "{}")) as Record<string, unknown>;
      this.edgeCalls.push({ endpoint, body });
      if (body.token !== this.token) return json({ ok: false, error: "invalid_auth" });
      const ids = Object.keys((body.updated_ids ?? {}) as Record<string, unknown>);
      const source = endpoint === "users/info" ? this.users : this.channels;
      return json({ ok: true, results: ids.map((id) => source.get(id)).filter(Boolean) });
    }
    if (url.hostname === "test.slack.com") {
      const method = url.pathname.replace("/api/", "");
      const req = new Request(url, init);
      const fd = await req.formData();
      const fields: Record<string, string> = {};
      fd.forEach((v, k) => {
        // Multipart encoding turns line breaks into CRLF; the server normalizes them back.
        fields[k] = String(v).replace(/\r\n/g, "\n");
      });
      this.calls.push({ method, fields });
      if (fields.token !== this.token) return json({ ok: false, error: "invalid_auth" });
      const handler = this.api.get(method);
      const n = (this.apiCounts.get(method) ?? 0) + 1;
      this.apiCounts.set(method, n);
      if (method === "conversations.info" && !handler) {
        const ch = this.channels.get(fields.channel ?? "");
        return ch ? json({ ok: true, channel: ch }) : json({ ok: false, error: "channel_not_found" });
      }
      if (!handler) return json({ ok: false, error: "unknown_method" });
      const result = await handler(fields, n);
      if (result instanceof Response) return result;
      return json(result);
    }
    if (url.hostname === "files.slack.com") {
      const f = this.fileBodies.get(url.pathname);
      if (!f) return new Response("missing", { status: 404 });
      return new Response(f.body, { status: 200, headers: { "content-type": f.contentType, "content-length": String(f.body.byteLength) } });
    }
    throw new Error(`unexpected fetch ${url.toString()}`);
  };

  readonly createWebSocket = (url: string, headers: Record<string, string>): WebSocketLike => {
    const sock = new FakeSocket(url, headers);
    this.sockets.push(sock);
    if (this.autoHello) {
      setImmediate(() => {
        sock.open();
        const n = this.sockets.length;
        const fast = typeof this.helloFast === "function" ? this.helloFast(n) : this.helloFast;
        sock.deliver({ type: "hello", fast_reconnect: fast, start: true });
      });
    }
    return sock;
  };
}

export function makeClient(slack: FakeSlack, extra: Partial<GabrielClientOptions> = {}): GabrielClient {
  return new GabrielClient({
    auth: { cookieD: COOKIE },
    hydration: { hydrateBudgetMs: 200 },
    ...extra,
    advanced: { endpoints: ENDPOINTS, fetch: slack.fetch as typeof fetch, createWebSocket: slack.createWebSocket, startTimeoutMs: 5_000, ...extra.advanced },
  });
}

export function tick(ms = 0): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

export async function until(cond: () => boolean, timeoutMs = 3_000, label = "condition"): Promise<void> {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > timeoutMs) throw new Error(`timed out waiting for ${label}`);
    await tick(5);
  }
}

export function messageFrame(over: Record<string, unknown> = {}): RawWsFrame {
  return {
    type: "message",
    channel: "C0GENERAL01",
    user: "U0ALICE0001",
    text: "hello",
    ts: "1800000001.000100",
    event_ts: "1800000001.000100",
    ...over,
  };
}
