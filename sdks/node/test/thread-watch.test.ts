import { afterEach, describe, expect, it } from "vitest";
import type { GabrielClient } from "../src/client/client.js";
import type { ConnectionGapEvent, MessageCreatedEvent, MessageThreadMetaUpdatedEvent } from "../src/events/types.js";
import { MemoryStateStore } from "../src/state/store.js";
import { FakeSlack, makeClient, messageFrame, tick, until } from "./harness.js";

const CH = "C0GENERAL01";
const ROOT = "1800000100.000100";
const OTHER_ROOT = "1800000200.000100";

class SharedStore extends MemoryStateStore {
  override readonly persistent = true;
}

let clients: GabrielClient[] = [];
afterEach(async () => {
  for (const c of clients) await c.stop({ drainMs: 100 }).catch(() => {});
  clients = [];
});

function newClient(slack: FakeSlack, extra: Parameters<typeof makeClient>[1] = {}): GabrielClient {
  const client = makeClient(slack, extra);
  clients.push(client);
  return client;
}

async function started(slack = new FakeSlack(), extra: Parameters<typeof makeClient>[1] = {}) {
  const client = newClient(slack, extra);
  await client.start();
  return { slack, client };
}

/** A thread whose replies honour `oldest` / `inclusive=false` like Slack does. */
function serveThread(slack: FakeSlack, replies: string[], root = ROOT): void {
  slack.on("conversations.replies", (f) => {
    if (f.ts !== root) return { ok: true, has_more: false, messages: [] };
    const newer = replies.filter((ts) => !f.oldest || Number(ts) > Number(f.oldest));
    return {
      ok: true,
      has_more: false,
      messages: [
        { type: "message", user: "U0BOB000001", text: "root", ts: root, thread_ts: root, reply_count: replies.length },
        ...newer.map((ts) => ({ type: "message", user: "U0ALICE0001", text: `reply ${ts}`, ts, thread_ts: root })),
      ],
    };
  });
}

const replyFrame = (ts: string, root = ROOT) => messageFrame({ ts, event_ts: ts, thread_ts: root, text: `reply ${ts}` });

const repliedFrame = (latest: string, root = ROOT) => ({
  type: "message",
  subtype: "message_replied",
  channel: CH,
  hidden: true,
  ts: `${latest.split(".")[0]}.000900`,
  event_ts: `${latest.split(".")[0]}.000900`,
  message: { type: "message", user: "U0BOB000001", text: "root", ts: root, thread_ts: root, reply_count: 3, latest_reply: latest },
});

/** Every Web API method name that writes, marks or subscribes. */
const WRITE_METHOD = /^(chat\.|subscriptions\.|reactions\.|files\.|conversations\.(mark|join|leave|open|close)|.*\.mark$)/;

async function coldReconnect(client: GabrielClient, slack: FakeSlack): Promise<void> {
  const sockets = slack.sockets.length;
  client.core.ws.markReady(false);
  slack.socket.serverClose(1006); // no fast-reconnect ticket → cold reconnect
  await until(() => slack.sockets.length === sockets + 1, 5_000, "reconnect");
}

describe("threads.watch (local-only thread watch)", () => {
  it("never sends a write request or a gateway subscription", async () => {
    const slack = new FakeSlack();
    serveThread(slack, ["1800000101.000100"]);
    const { client } = await started(slack);
    const before = slack.calls.length;
    const sentBefore = slack.socket.sent.length;
    const res = await client.threads.watch({ channel: CH, threadTs: ROOT });
    client.threads.unwatch({ channel: CH, threadTs: ROOT });
    const methods = slack.calls.slice(before).map((c) => c.method);
    expect(res).toEqual({ recovered: 1, complete: true });
    expect(methods).toEqual(["conversations.replies"]);
    expect(methods.filter((m) => WRITE_METHOD.test(m))).toEqual([]);
    expect(slack.socket.sent.slice(sentBefore).map((s) => s.type).filter((t) => t !== "ping")).toEqual([]);
  });

  it("backfills only replies after `after`, de-duplicated against realtime, and waits for the handlers", async () => {
    const slack = new FakeSlack();
    serveThread(slack, ["1800000101.000100", "1800000102.000100", "1800000103.000100"]);
    const { client } = await started(slack);
    const seen: Array<{ ts: string; delivery: string; origin: string }> = [];
    let handled = 0;
    client.on("message.created", async (e) => {
      seen.push({ ts: e.ts, delivery: e.delivery, origin: e.origin });
      await tick(30);
      handled++;
    });
    slack.socket.deliver(replyFrame("1800000103.000100"));
    await until(() => handled === 1);

    const res = await client.threads.watch({ channel: CH, threadTs: ROOT, after: "1800000101.000100" });
    expect(res).toEqual({ recovered: 1, complete: true });
    expect(handled).toBe(2); // resolved only after the backfilled event's handler finished
    expect(slack.callsTo("conversations.replies")[0]!.fields).toMatchObject({ channel: CH, ts: ROOT, oldest: "1800000101.000100", inclusive: "false" });
    expect(seen).toEqual([
      { ts: "1800000103.000100", delivery: "realtime", origin: "other_user" },
      { ts: "1800000102.000100", delivery: "backfill", origin: "other_user" },
    ]);
    expect(client.threads.watched()).toEqual([{ channel: CH, threadTs: ROOT, watermark: "1800000103.000100" }]);

    // Repeating the call is idempotent: nothing new is delivered.
    expect(await client.threads.watch({ channel: CH, threadTs: ROOT })).toEqual({ recovered: 0, complete: true });
    expect(seen).toHaveLength(2);
  });

  it("only ever raises the watermark", async () => {
    const { client } = await started();
    await client.threads.watch({ channel: CH, threadTs: ROOT, after: "1800000150.000100" });
    await client.threads.watch({ channel: CH, threadTs: ROOT, after: "1800000120.000100" });
    expect(client.threads.watched()).toEqual([{ channel: CH, threadTs: ROOT, watermark: "1800000150.000100" }]);
  });

  it("rejects invalid arguments and does not throw on API failures", async () => {
    const slack = new FakeSlack();
    slack.on("conversations.replies", () => ({ ok: false, error: "channel_not_found" }));
    const { client } = await started(slack);
    await expect(client.threads.watch({ channel: "not-a-channel", threadTs: ROOT })).rejects.toMatchObject({ code: "INVALID_ARGUMENT" });
    await expect(client.threads.watch({ channel: CH, threadTs: "yesterday" })).rejects.toMatchObject({ code: "INVALID_ARGUMENT" });
    await expect(client.threads.watch({ channel: CH, threadTs: ROOT, after: "nope" })).rejects.toMatchObject({ code: "INVALID_ARGUMENT" });
    expect(client.threads.watched()).toEqual([]);

    expect(await client.threads.watch({ channel: CH, threadTs: ROOT })).toEqual({ recovered: 0, complete: false });
    expect(client.threads.watched()).toEqual([{ channel: CH, threadTs: ROOT, watermark: ROOT }]);
  });

  it("needs a started client", async () => {
    const client = newClient(new FakeSlack());
    await expect(client.threads.watch({ channel: CH, threadTs: ROOT })).rejects.toMatchObject({ code: "INVALID_STATE" });
  });

  it("keeps recovering the thread after a restart with the same state store", async () => {
    const store = new SharedStore();
    const slack = new FakeSlack();
    serveThread(slack, []);
    const first = await started(slack, { stateStore: store });
    await first.client.threads.watch({ channel: CH, threadTs: ROOT, after: "1800000101.000100" });
    await first.client.stop({ drainMs: 100 });

    const slack2 = new FakeSlack();
    serveThread(slack2, ["1800000101.000100", "1800000104.000100"]);
    const client = newClient(slack2, { stateStore: store });
    const seen: Array<{ ts: string; delivery: string }> = [];
    const gaps: ConnectionGapEvent[] = [];
    client.on("message.created", (e) => void seen.push({ ts: e.ts, delivery: e.delivery }));
    client.on("connection.gap", (e) => void gaps.push(e));
    await client.start();
    await until(() => seen.length === 1 && gaps.length === 1);
    expect(seen).toEqual([{ ts: "1800000104.000100", delivery: "backfill" }]);
    expect(slack2.callsTo("conversations.replies")[0]!.fields).toMatchObject({ ts: ROOT, oldest: "1800000101.000100" });
    expect(gaps[0]).toMatchObject({ cause: "cold_resync", recovered: true, threadsRecovered: [{ channel: CH, threadTs: ROOT }] });
    await until(() => client.threads.watched()[0]?.watermark === "1800000104.000100");
    expect(slack2.calls.filter((c) => WRITE_METHOD.test(c.method))).toEqual([]);
  });

  it("fetches the thread when message_replied announces a reply that never arrives", async () => {
    const slack = new FakeSlack();
    const replies: string[] = [];
    serveThread(slack, replies);
    const { client } = await started(slack, { recovery: { threadReplyGraceMs: 50 } });
    const created: MessageCreatedEvent[] = [];
    const meta: MessageThreadMetaUpdatedEvent[] = [];
    client.on("message.created", (e) => void created.push(e));
    client.on("message.thread_meta_updated", (e) => void meta.push(e));
    await client.threads.watch({ channel: CH, threadTs: ROOT });
    const fetches = () => slack.callsTo("conversations.replies").length;
    expect(fetches()).toBe(1);

    replies.push("1800000105.000100");
    slack.socket.deliver(repliedFrame("1800000105.000100"));
    await until(() => created.length === 1);
    expect(meta).toHaveLength(1); // the original event is still emitted
    expect(created[0]).toMatchObject({ ts: "1800000105.000100", delivery: "backfill", isThreadReply: true });
    expect(fetches()).toBe(2);
    expect(slack.callsTo("conversations.replies")[1]!.fields.oldest).toBe(ROOT);

    // The reply arrives in realtime within the grace period: no fetch.
    replies.push("1800000106.000100");
    slack.socket.deliver(repliedFrame("1800000106.000100"));
    slack.socket.deliver(replyFrame("1800000106.000100"));
    await until(() => created.length === 2);
    await tick(120);
    expect(fetches()).toBe(2);
    expect(created[1]).toMatchObject({ ts: "1800000106.000100", delivery: "realtime" });

    // Threads that are not explicitly watched are left alone.
    slack.socket.deliver(repliedFrame("1800000201.000100", OTHER_ROOT));
    await tick(120);
    expect(fetches()).toBe(2);
  });

  it("is not evicted by the activity LRU and is bounded on its own", async () => {
    const slack = new FakeSlack();
    slack.on("subscriptions.thread.add", () => ({ ok: true }));
    const { client } = await started(slack, { recovery: { watchedThreadsLimit: 1, maxExplicitThreadWatches: 2 } });
    await client.threads.watch({ channel: CH, threadTs: ROOT });
    await client.threads.subscribe({ channel: CH, threadTs: "1800000300.000100" });
    await client.threads.subscribe({ channel: CH, threadTs: "1800000400.000100" }); // evicts 300 from the LRU
    expect(client.threads.watched().map((t) => t.threadTs)).toEqual([ROOT]);

    await client.threads.watch({ channel: CH, threadTs: OTHER_ROOT });
    await expect(client.threads.watch({ channel: CH, threadTs: "1800000500.000100" })).rejects.toMatchObject({ code: "SUBSCRIPTION_CAP_EXCEEDED" });
    await expect(client.threads.watch({ channel: CH, threadTs: ROOT })).resolves.toMatchObject({ complete: true }); // already watched

    const before = slack.callsTo("conversations.replies").length;
    await coldReconnect(client, slack);
    await until(() => slack.callsTo("conversations.replies").length >= before + 3);
    await tick(20);
    const fetched = slack.callsTo("conversations.replies").slice(before).map((c) => c.fields.ts).sort();
    expect(fetched).toEqual([ROOT, OTHER_ROOT, "1800000400.000100"].sort());
  });

  it("stops recovering the thread after unwatch", async () => {
    const slack = new FakeSlack();
    serveThread(slack, []);
    const { client } = await started(slack, { recovery: { threadReplyGraceMs: 20 } });
    await client.threads.watch({ channel: CH, threadTs: ROOT });
    const callsBefore = slack.calls.length;
    client.threads.unwatch({ channel: CH, threadTs: ROOT });
    client.threads.unwatch({ channel: CH, threadTs: ROOT }); // idempotent
    expect(slack.calls.length).toBe(callsBefore); // unwatch calls nothing on Slack
    expect(client.threads.watched()).toEqual([]);

    const repliesBefore = slack.callsTo("conversations.replies").length;
    slack.socket.deliver(repliedFrame("1800000107.000100"));
    await tick(80);
    await coldReconnect(client, slack);
    await tick(100);
    expect(slack.callsTo("conversations.replies").length).toBe(repliesBefore);
  });

  it("does not deadlock when called from inside a handler of the same thread", async () => {
    const slack = new FakeSlack();
    serveThread(slack, ["1800000101.000100", "1800000102.000100"]);
    const { client } = await started(slack);
    const seen: string[] = [];
    let result: unknown;
    client.on("message.created", async (e) => {
      seen.push(e.ts);
      if (e.delivery === "realtime") result = await client.threads.watch({ channel: CH, threadTs: ROOT });
    });
    slack.socket.deliver(replyFrame("1800000102.000100"));
    await until(() => result !== undefined && seen.length === 2);
    expect(result).toEqual({ recovered: 1, complete: true });
    expect(seen).toEqual(["1800000102.000100", "1800000101.000100"]);
  });
});
