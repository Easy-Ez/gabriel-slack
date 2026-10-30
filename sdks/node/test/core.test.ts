import { afterEach, describe, expect, it } from "vitest";
import { GabrielCore } from "../src/core/core.js";
import { FIRST_SEQ_ID } from "../src/core/ws.js";
import { COOKIE, ENDPOINTS, FakeSlack, TEAM, tick, until } from "./harness.js";

function makeCore(slack: FakeSlack) {
  return new GabrielCore({
    auth: { cookieD: COOKIE },
    endpoints: ENDPOINTS,
    fetch: slack.fetch as typeof fetch,
    createWebSocket: slack.createWebSocket,
    maxHttpRetries: 2,
  });
}

let cores: GabrielCore[] = [];
afterEach(() => {
  for (const c of cores) c.disconnect();
  cores = [];
});

describe("auth + http", () => {
  it("derives the workspace session from the cookie alone", async () => {
    const slack = new FakeSlack();
    const core = makeCore(slack);
    const session = await core.initialize();
    expect(session.teamId).toBe(TEAM);
    expect(session.apiBase).toBe("https://test.slack.com/");
    expect(slack.authCalls).toBe(1);
  });

  it("collapses 20 concurrent invalid_auth failures into one token exchange and replays", async () => {
    const slack = new FakeSlack();
    slack.on("auth.test", () => ({ ok: true }));
    const core = makeCore(slack);
    await core.initialize();
    slack.token = "xoxc-rotated-2";
    const results = await Promise.all(Array.from({ length: 20 }, () => core.callWebApi("auth.test")));
    expect(results.every((r) => r.ok === true)).toBe(true);
    expect(slack.authCalls).toBe(2); // initial + exactly one refresh
  });

  it("enters AUTH_DEAD when the cookie is no longer signed in", async () => {
    const slack = new FakeSlack();
    slack.on("auth.test", () => ({ ok: true }));
    const core = makeCore(slack);
    await core.initialize();
    const dead: string[] = [];
    core.onAuthEvent((e) => dead.push(e.type));
    slack.token = "xoxc-rotated-3";
    slack.authAlive = false;
    await expect(core.callWebApi("auth.test")).rejects.toMatchObject({ code: "AUTH_DEAD" });
    expect(core.authManager.state).toBe("AUTH_DEAD");
    expect(dead).toEqual(["dead"]);
    await expect(core.callWebApi("auth.test")).rejects.toMatchObject({ code: "AUTH_DEAD" });
  });

  it("does not treat an outdated User-Agent rejection as a dead session", async () => {
    const slack = new FakeSlack();
    const orig = slack.fetch;
    (slack as { fetch: typeof slack.fetch }).fetch = async (input, init) =>
      String(input).includes("app.slack.test") ? new Response("Sorry, this browser is not supported", { status: 403 }) : orig(input, init);
    const core = makeCore(slack);
    await expect(core.initialize()).rejects.toMatchObject({ code: "INVALID_STATE" });
    expect(core.authManager.state).not.toBe("AUTH_DEAD");
  });

  it("shares a 429 cool-down per method and retries", async () => {
    const slack = new FakeSlack();
    slack.on("conversations.history", (_f, n) =>
      n === 1 ? new Response(JSON.stringify({ ok: false, error: "ratelimited" }), { status: 429, headers: { "retry-after": "1" } }) : { ok: true, messages: [] },
    );
    const core = makeCore(slack);
    await core.initialize();
    const started = Date.now();
    const res = await core.callWebApi("conversations.history", { channel: "C0GENERAL01" });
    expect(res.ok).toBe(true);
    expect(Date.now() - started).toBeGreaterThanOrEqual(900);
  });

  it("does not retry non-idempotent writes on server errors", async () => {
    const slack = new FakeSlack();
    slack.on("chat.delete", () => new Response("oops", { status: 502 }));
    const core = makeCore(slack);
    await core.initialize();
    await expect(core.callWebApi("chat.delete", { channel: "C0GENERAL01", ts: "1.1" })).rejects.toMatchObject({ code: "SERVER_ERROR" });
    expect(slack.callsTo("chat.delete")).toHaveLength(1);
  });

  it("rejects a workspace value that would redirect credentials", async () => {
    const slack = new FakeSlack();
    const core = new GabrielCore({
      auth: { cookieD: COOKIE, token: "xoxc-test-token-1", workspace: "evil.example#" },
      endpoints: ENDPOINTS,
      fetch: slack.fetch as typeof fetch,
      createWebSocket: slack.createWebSocket,
    });
    await expect(core.initialize()).rejects.toMatchObject({ code: "INVALID_ARGUMENT" });
  });

  it("refuses to send credentials to non-Slack hosts", async () => {
    const slack = new FakeSlack();
    const core = makeCore(slack);
    await core.initialize();
    await expect(core.http.fetchAuthenticated("https://evil.example.com/x", { method: "GET" })).rejects.toMatchObject({ code: "INVALID_ARGUMENT" });
  });
});

describe("websocket lifecycle", () => {
  it("connects with cookie headers, handshake params, and correlates reply_to from 16385", async () => {
    const slack = new FakeSlack();
    const core = makeCore(slack);
    cores.push(core);
    await core.connect();
    await until(() => core.ws.state === "RESYNCING" || core.ws.state === "READY");
    const sock = slack.socket;
    expect(sock.headers.Cookie).toBe(`d=${COOKIE}`);
    const url = new URL(sock.url);
    expect(url.searchParams.get("batch_presence_aware")).toBe("1");
    expect(url.searchParams.get("flannel")).toBe("3");
    expect(url.searchParams.get("gateway_server")).toBe(`${TEAM}-1`);
    const p = core.sendWsCommand({ type: "subscription_update", subtype: "user_change_subscribe_request", ids: ["U0ALICE0001"] });
    const sent = sock.sent.at(-1)!;
    expect(sent.id).toBe(FIRST_SEQ_ID);
    sock.deliver({ ok: true, reply_to: sent.id, type: "subscription_update" });
    await expect(p).resolves.toMatchObject({ reply_to: FIRST_SEQ_ID });
  });

  it("rejects every pending command immediately when the socket closes", async () => {
    const slack = new FakeSlack();
    const core = makeCore(slack);
    cores.push(core);
    await core.connect();
    await until(() => core.ws.isConnected());
    const pending = [core.sendWsCommand({ type: "a" }), core.sendWsCommand({ type: "b" })];
    slack.socket.serverClose(1006);
    for (const p of pending) await expect(p).rejects.toMatchObject({ code: "SOCKET_CLOSED" });
  });

  it("uses the reconnect_url fast path after a drop and the cold path after a fast failure", async () => {
    const slack = new FakeSlack();
    const core = makeCore(slack);
    cores.push(core);
    await core.connect();
    await until(() => core.ws.isConnected());
    slack.socket.deliver({ type: "reconnect_url", url: "wss://ws.slack.test/?frt=TICKET" });
    core.ws.markReady(false);
    slack.socket.serverClose(1006);
    await until(() => slack.sockets.length === 2);
    expect(slack.sockets[1]!.url).toContain("frt=TICKET");
    await until(() => core.ws.isConnected());
    core.ws.markReady(false);
    // Fast connection fails before hello → ticket discarded → next attempt is cold.
    slack.autoHello = false;
    slack.socket.serverClose(1006);
    await until(() => slack.sockets.length === 3);
    slack.sockets[2]!.serverClose(1006); // failed before hello
    await until(() => slack.sockets.length === 4, 5_000);
    expect(slack.sockets[3]!.url).not.toContain("frt=");
  });

  it("closes with 4104 on goodbye and reconnects", async () => {
    const slack = new FakeSlack();
    const core = makeCore(slack);
    cores.push(core);
    await core.connect();
    await until(() => core.ws.isConnected());
    core.ws.markReady(false);
    const first = slack.socket;
    first.deliver({ type: "goodbye" });
    await until(() => slack.sockets.length === 2);
    expect(first.closedWith).toBe(4104);
  });

  it("reports unknown frame types once as protocol drift", async () => {
    const slack = new FakeSlack();
    const core = makeCore(slack);
    cores.push(core);
    const drift: string[] = [];
    core.onProtocolDrift((d) => drift.push(d.unknownType));
    await core.connect();
    await until(() => core.ws.isConnected());
    slack.socket.deliver({ type: "brand_new_event" });
    slack.socket.deliver({ type: "brand_new_event" });
    await tick(5);
    expect(drift).toEqual(["brand_new_event"]);
  });
});
