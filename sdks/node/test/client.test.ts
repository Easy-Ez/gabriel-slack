import { afterEach, describe, expect, it } from "vitest";
import type { GabrielClient } from "../src/client/client.js";
import type { GabrielEvent, MessageCreatedEvent } from "../src/events/types.js";
import { FakeSlack, SELF, makeClient, messageFrame, tick, until } from "./harness.js";

let clients: GabrielClient[] = [];
afterEach(async () => {
  for (const c of clients) await c.stop({ drainMs: 100 }).catch(() => {});
  clients = [];
});

async function started(slack = new FakeSlack(), extra: Parameters<typeof makeClient>[1] = {}) {
  const client = makeClient(slack, extra);
  clients.push(client);
  await client.start();
  return { slack, client };
}

describe("GabrielClient events", () => {
  it("starts from the cookie and emits connection.ready", async () => {
    const slack = new FakeSlack();
    const client = makeClient(slack);
    clients.push(client);
    const ready: GabrielEvent[] = [];
    client.on("connection.ready", (e) => void ready.push(e));
    await client.start();
    await until(() => ready.length === 1);
    expect(client.selfUserId).toBe(SELF);
    expect(client.health().status).toBe("healthy");
  });

  it("delivers hydrated message.created with mentions-me via my user group", async () => {
    const { slack, client } = await started();
    const got: MessageCreatedEvent[] = [];
    client.on("message.created", (e) => void got.push(e));
    slack.socket.deliver(messageFrame({ text: "<!subteam^S0ONCALL001|@oncall> please look <@U0BOB000001>" }));
    await until(() => got.length === 1);
    const evt = got[0]!;
    expect(evt.sender.displayName).toBe("Alice");
    expect(evt.channel.name).toBe("general");
    expect(evt.markdown).toBe("@oncall please look @Bob");
    expect(evt.isMentioningMe).toBe(true);
    expect(evt.toLLMContext()).toContain('<slack_message untrusted="true">');
    expect(JSON.parse(JSON.stringify(evt)).toLLMContext).toBeUndefined();
  });

  it("keeps per-channel dispatch order even when hydration finishes out of order", async () => {
    const slack = new FakeSlack();
    slack.users.set("U0SLOW00001", { id: "U0SLOW00001", name: "slow", profile: { display_name: "Slow" } });
    const origFetch = slack.fetch;
    (slack as { fetch: typeof slack.fetch }).fetch = async (input, init) => {
      if (String(init?.body ?? "").includes("U0SLOW00001")) await tick(120);
      return origFetch(input, init);
    };
    const { client } = await started(slack);
    const order: string[] = [];
    client.on("message.created", (e) => void order.push(e.ts));
    slack.socket.deliver(messageFrame({ user: "U0SLOW00001", ts: "1800000001.000001" }));
    slack.socket.deliver(messageFrame({ ts: "1800000001.000002" }));
    slack.socket.deliver(messageFrame({ ts: "1800000001.000003" }));
    await until(() => order.length === 3);
    expect(order).toEqual(["1800000001.000001", "1800000001.000002", "1800000001.000003"]);
  });

  it("de-duplicates redelivered frames by eventId", async () => {
    const { slack, client } = await started();
    let n = 0;
    client.on("message.created", () => void n++);
    slack.socket.deliver(messageFrame());
    slack.socket.deliver(messageFrame());
    await tick(80);
    expect(n).toBe(1);
  });

  it("filters echoes of this client's own messages but not messages typed elsewhere", async () => {
    const { slack, client } = await started();
    const origins: string[] = [];
    client.on("message.created", (e) => void origins.push(e.origin));
    const { ts } = await client.chat.send({ channel: "C0GENERAL01", markdown: "from sdk" });
    const cmid = slack.callsTo("chat.postMessage")[0]!.fields.client_msg_id;
    slack.socket.deliver(messageFrame({ user: SELF, ts, client_msg_id: cmid, text: "from sdk" }));
    slack.socket.deliver(messageFrame({ user: SELF, ts: "1800000009.000001", text: "typed in browser" }));
    await until(() => origins.length === 1);
    await tick(50);
    expect(origins).toEqual(["self_other_client"]);
  });

  it("routes unmodelled message subtypes to message.other and badge snapshots once", async () => {
    const { slack, client } = await started();
    const types: string[] = [];
    client.on("message.other", (e) => void types.push(e.type));
    client.on("unread.badge_counts", (e) => void types.push(`${e.type}:${e.mentionCount}`));
    slack.socket.deliver(messageFrame({ subtype: "channel_join" }));
    const badge = { type: "badge_counts_updated", activity_v2: { at_user: 1, thread_v2: 2 }, event_ts: "1" };
    slack.socket.deliver(badge);
    slack.socket.deliver({ ...badge, event_ts: "2" });
    await until(() => types.length === 2);
    await tick(30);
    // Different lanes: no cross-lane ordering guarantee.
    expect(types.sort()).toEqual(["message.other", "unread.badge_counts:1"]);
  });

  it("enriches reaction targets with the thread root", async () => {
    const slack = new FakeSlack();
    slack.on("conversations.replies", () => ({ ok: true, messages: [{ ts: "1800000000.000100", thread_ts: "1800000000.000100" }] }));
    const { client } = await started(slack);
    const got: Array<{ ts?: string; root?: string }> = [];
    client.on("interaction.reaction_added", (e) => void got.push({ ts: e.target.ts, root: e.target.threadRootTs }));
    slack.socket.deliver({ type: "reaction_added", user: SELF, reaction: "memo", item: { type: "message", channel: "C0GENERAL01", ts: "1800000002.000100" }, event_ts: "1800000003.000000" });
    await until(() => got.length === 1);
    expect(got[0]).toEqual({ ts: "1800000002.000100", root: "1800000000.000100" });
  });

  it("isolates handler errors and routes them to onError", async () => {
    const { slack, client } = await started();
    const errors: unknown[] = [];
    let after = 0;
    client.onError((err) => errors.push(err));
    client.on("message.created", (e) => {
      if (e.ts === "1800000001.000001") throw new Error("boom");
      after++;
    });
    slack.socket.deliver(messageFrame({ ts: "1800000001.000001" }));
    slack.socket.deliver(messageFrame({ ts: "1800000001.000002" }));
    await until(() => after === 1);
    expect(String(errors[0])).toContain("boom");
  });

  it("aborts ctx.signal immediately when the trigger message is deleted", async () => {
    const { slack, client } = await started();
    let aborted = false;
    client.on("message.created", async (_e, ctx) => {
      await new Promise<void>((resolve) => ctx.signal.addEventListener("abort", () => resolve()));
      aborted = true;
    });
    slack.socket.deliver(messageFrame());
    await tick(50);
    slack.socket.deliver({ type: "message", subtype: "message_deleted", channel: "C0GENERAL01", deleted_ts: "1800000001.000100", ts: "1800000099.000000" });
    await until(() => aborted);
  });

  it("supports pull-mode iteration", async () => {
    const { slack, client } = await started();
    const it = client.events();
    slack.socket.deliver(messageFrame());
    for await (const evt of it) {
      if (evt.type === "message.created") break;
    }
  });
});

describe("handler timeouts", () => {
  it("times out slow handlers via ctx.signal and HANDLER_TIMEOUT", async () => {
    const { slack, client } = await started(new FakeSlack(), { concurrency: { handlerTimeoutMs: 50 } });
    const codes: string[] = [];
    const handled: string[] = [];
    client.onError((err) => codes.push((err as { code: string }).code));
    client.on("message.created", async (evt, ctx) => {
      if (evt.ts === "1800000001.000001") {
        await new Promise<void>((resolve) => ctx.signal.addEventListener("abort", () => resolve()));
      }
      handled.push(evt.ts);
    });
    slack.socket.deliver(messageFrame({ ts: "1800000001.000001" }));
    slack.socket.deliver(messageFrame({ ts: "1800000001.000002" }));
    await until(() => handled.length === 2);
    expect(codes).toEqual(["HANDLER_TIMEOUT"]);
    expect(handled).toEqual(["1800000001.000001", "1800000001.000002"]);
  });
});

describe("ctx helpers", () => {
  it("replies in thread and waits for a follow-up without deadlocking the channel", async () => {
    const { slack, client } = await started();
    let answer: string | undefined;
    let otherHandled = false;
    client.on("message.created", async (evt, ctx) => {
      if (evt.markdown === "!deploy") {
        await ctx.reply("confirm?");
        const reply = await ctx.waitForReply((r) => r.threadTs === evt.ts && /^(yes|no)$/.test(r.markdown), { timeoutMs: 2_000 });
        answer = reply?.markdown;
      } else if (evt.markdown === "unrelated") {
        otherHandled = true;
      }
    });
    slack.socket.deliver(messageFrame({ text: "!deploy", ts: "1800000001.000100" }));
    await until(() => slack.callsTo("chat.postMessage").length === 1);
    expect(slack.callsTo("chat.postMessage")[0]!.fields.thread_ts).toBe("1800000001.000100");
    slack.socket.deliver(messageFrame({ text: "unrelated", ts: "1800000001.000200" }));
    await until(() => otherHandled);
    slack.socket.deliver(messageFrame({ text: "yes", ts: "1800000001.000300", thread_ts: "1800000001.000100" }));
    await until(() => answer !== undefined);
    expect(answer).toBe("yes");
  });

  it("trips the loop breaker for automatic replies", async () => {
    const { slack, client } = await started(new FakeSlack(), { loopGuard: { maxAutoRepliesPerMinute: 2 } });
    const codes: string[] = [];
    let loops = 0;
    client.on("guard.loop_detected", () => void loops++);
    client.on("message.created", async (_e, ctx) => {
      await ctx.reply("auto").catch((err: { code: string }) => codes.push(err.code));
    });
    for (let i = 1; i <= 3; i++) slack.socket.deliver(messageFrame({ ts: `1800000001.00000${i}`, thread_ts: "1800000000.000001" }));
    await until(() => codes.length === 1);
    expect(codes).toEqual(["LOOP_DETECTED"]);
    expect(loops).toBe(1);
    expect(slack.callsTo("chat.postMessage")).toHaveLength(2);
  });
});

describe("chat", () => {
  it("auto-chunks oversized markdown in FIFO order with unique client_msg_id", async () => {
    const { slack, client } = await started();
    const para = "lorem ipsum ".repeat(500);
    const res = await client.chat.send({ channel: "C0GENERAL01", markdown: Array.from({ length: 6 }, () => para).join("\n\n") });
    const calls = slack.callsTo("chat.postMessage");
    expect(calls.length).toBeGreaterThan(1);
    expect(res.chunkTs).toHaveLength(calls.length);
    expect(new Set(calls.map((c) => c.fields.client_msg_id)).size).toBe(calls.length);
    for (const c of calls) expect(Array.from(c.fields.markdown_text!).length).toBeLessThanOrEqual(11_500);
  });

  it("re-splits at 0.8x when the server still says msg_too_long", async () => {
    const slack = new FakeSlack();
    let first = true;
    slack.on("chat.postMessage", (f) => {
      if (first) {
        first = false;
        return { ok: false, error: "msg_too_long" };
      }
      return { ok: true, ts: `${1800000000 + Math.floor(Math.random() * 1e6)}.000100`, channel: f.channel };
    });
    const { client } = await started(slack);
    const res = await client.chat.send({ channel: "C0GENERAL01", markdown: "x ".repeat(5000) });
    expect(res.chunkTs.length).toBeGreaterThan(1);
  });

  it("restores the unread cursor after posting when the channel had unreads", async () => {
    const slack = new FakeSlack();
    slack.on("conversations.info", () => ({ ok: true, channel: { id: "C0GENERAL01", last_read: "1800000000.000001", unread_count: 3 } }));
    slack.on("conversations.mark", () => ({ ok: true }));
    const { client } = await started(slack);
    await Promise.all([
      client.chat.send({ channel: "C0GENERAL01", markdown: "a", preserveUnreadCursor: true }),
      client.chat.send({ channel: "C0GENERAL01", markdown: "b", preserveUnreadCursor: true }),
    ]);
    const marks = slack.callsTo("conversations.mark");
    expect(marks).toHaveLength(1);
    expect(marks[0]!.fields.ts).toBe("1800000000.000001");
  });

  it("streams with a single in-flight update and lands the final content", async () => {
    const slack = new FakeSlack();
    const updates: string[] = [];
    slack.on("chat.update", async (f) => {
      updates.push(f.markdown_text!);
      await tick(30);
      return { ok: true, ts: f.ts, channel: f.channel };
    });
    const { client } = await started(slack, { chat: { streamUpdatesPerSecond: 100, streamMinIntervalMs: 0 } });
    const stream = await client.chat.createStream({ channel: "C0GENERAL01", initialMarkdown: "thinking" });
    for (let i = 0; i < 50; i++) {
      stream.append(`${i} `);
      await tick(1);
    }
    stream.append("```js\nconst x = 1;");
    const done = await stream.finish();
    expect(done.ts).toBe(stream.ts);
    expect(updates.length).toBeLessThan(50);
    expect(updates.at(-1)).toContain("49 ```js\nconst x = 1;");
    expect(() => stream.append("late")).toThrow(/STREAM_CLOSED|already finished/);
  });

  it("continues a stream in a new message past the length budget", async () => {
    const slack = new FakeSlack();
    const finals = new Map<string, string>();
    slack.on("chat.update", (f) => {
      finals.set(f.ts!, f.markdown_text!);
      return { ok: true, ts: f.ts, channel: f.channel };
    });
    const { client } = await started(slack, { chat: { streamUpdatesPerSecond: 1000, streamMinIntervalMs: 0 } });
    const stream = await client.chat.createStream({ channel: "C0GENERAL01", threadTs: "1800000000.000100" });
    const paragraph = "token ".repeat(300).trim();
    let expected = "";
    for (let i = 0; i < 12; i++) {
      const chunk = `${paragraph} ${i}\n\n`;
      expected += chunk;
      stream.append(chunk);
    }
    const done = await stream.finish();
    expect(done.continuationTs.length).toBeGreaterThanOrEqual(1);
    const posts = slack.callsTo("chat.postMessage");
    expect(posts.slice(1).every((p) => p.fields.thread_ts === "1800000000.000100")).toBe(true);
    const all = [done.ts, ...done.continuationTs].map((ts) => finals.get(ts) ?? "").join("\n\n");
    expect(all.replace(/\s+/g, " ").trim()).toBe(expected.replace(/\s+/g, " ").trim());
    for (const text of finals.values()) expect(Array.from(text).length).toBeLessThanOrEqual(11_500);
  });

  it("keeps text appended while a continuation message is being posted", async () => {
    const slack = new FakeSlack();
    const finals = new Map<string, string>();
    slack.on("chat.update", (f) => {
      finals.set(f.ts!, f.markdown_text!);
      return { ok: true, ts: f.ts, channel: f.channel };
    });
    let posts = 0;
    let onSecondPost: (() => void) | undefined;
    slack.on("chat.postMessage", async (f) => {
      posts++;
      if (posts === 2) {
        onSecondPost?.();
        await tick(40);
      }
      const ts = `${1800000100 + posts}.000100`;
      if (posts > 1) finals.set(ts, f.markdown_text!);
      return { ok: true, ts, channel: f.channel };
    });
    const { client } = await started(slack, { chat: { streamUpdatesPerSecond: 1000, streamMinIntervalMs: 0 } });
    const stream = await client.chat.createStream({ channel: "C0GENERAL01" });
    onSecondPost = () => stream.append(" TAIL-APPENDED-DURING-ROLLOVER");
    stream.append(`${"word ".repeat(2600)}\n\n${"more ".repeat(400)}`);
    await until(() => posts >= 2);
    await tick(80);
    const done = await stream.finish();
    const all = [done.ts, ...done.continuationTs].map((ts) => finals.get(ts) ?? "").join(" ");
    expect(all).toContain("TAIL-APPENDED-DURING-ROLLOVER");
  });

  it("aborts the stream when its message is deleted in Slack", async () => {
    const { slack, client } = await started();
    const stream = await client.chat.createStream({ channel: "C0GENERAL01" });
    slack.socket.deliver({ type: "message", subtype: "message_deleted", channel: "C0GENERAL01", deleted_ts: stream.ts, ts: "1800009999.000000" });
    await until(() => stream.signal.aborted);
  });

  it("retries message_not_found within the 2s creation window", async () => {
    const slack = new FakeSlack();
    slack.on("chat.update", (f, n) => (n < 3 ? { ok: false, error: "message_not_found" } : { ok: true, ts: f.ts }));
    const { client } = await started(slack);
    const { ts } = await client.chat.send({ channel: "C0GENERAL01", markdown: "x" });
    await client.chat.update({ channel: "C0GENERAL01", ts, markdown: "y" });
    expect(slack.callsTo("chat.update")).toHaveLength(3);
  });
});

describe("presence", () => {
  it("sends the full set, emits initial snapshots once, and dedupes reconnect snapshots", async () => {
    const { slack, client } = await started();
    const events: Array<{ id: string; p: string; initial: boolean }> = [];
    client.on("presence.changed", (e) => void events.push({ id: e.user.id, p: e.presence, initial: e.isInitialSnapshot }));
    await client.presence.watch(["U0ALICE0001"]);
    await client.presence.watch(["U0BOB000001"]);
    await until(() => slack.socket.sentOfType("presence_sub").length >= 1);
    await tick(150);
    const subs = slack.socket.sentOfType("presence_sub");
    expect(subs.at(-1)!.ids).toEqual(["U0ALICE0001", "U0BOB000001"]);
    slack.socket.deliver({ type: "presence_change", presence: "active", users: ["U0ALICE0001", "U0BOB000001"] });
    await until(() => events.length === 2);
    expect(events.every((e) => e.initial)).toBe(true);

    // Reconnect: snapshot replays must not re-emit unchanged users.
    client.core.ws.markReady(false);
    slack.socket.serverClose(1006);
    await until(() => slack.sockets.length === 2 && slack.socket.sentOfType("presence_sub").length === 1, 5_000);
    slack.socket.deliver({ type: "presence_change", presence: "active", users: ["U0ALICE0001"] });
    slack.socket.deliver({ type: "presence_change", presence: "away", users: ["U0BOB000001"] });
    await until(() => events.length === 3);
    await tick(30);
    expect(events[2]).toEqual({ id: "U0BOB000001", p: "away", initial: false });
  });

  it("enforces the 16,000-byte presence frame cap", async () => {
    const { client } = await started();
    const ids = Array.from({ length: 1300 }, (_, i) => `U${String(i).padStart(10, "0")}`);
    await expect(client.presence.watch(ids)).rejects.toMatchObject({ code: "SUBSCRIPTION_CAP_EXCEEDED" });
  });

  it("unsubscribes when the last handle is released", async () => {
    const { slack, client } = await started();
    const a = await client.presence.watch(["U0ALICE0001"]);
    const b = await client.presence.watch(["U0ALICE0001"]);
    await tick(150);
    await a.unsubscribe();
    await tick(150);
    expect(slack.socket.sentOfType("presence_sub").at(-1)!.ids).toEqual(["U0ALICE0001"]);
    await b.unsubscribe();
    await tick(150);
    expect(slack.socket.sentOfType("presence_sub").at(-1)!.ids).toEqual([]);
  });
});

describe("scope sandbox", () => {
  it("allows trigger-thread writes and rejects everything else", async () => {
    const { slack, client } = await started();
    const agent = client.scope({
      read: { channels: ["C0GENERAL01"], allowSearch: false },
      write: { targetScope: "trigger_thread_only", allowedActions: ["chat.send", "reactions.add"], disclosureSuffix: "\n_ai_" },
    });
    await expect(agent.chat.send({ channel: "C0GENERAL01", markdown: "outside handler" })).rejects.toMatchObject({ code: "SCOPE_POLICY_VIOLATION" });
    expect(() => agent.core).toThrow(/not available/);
    const results: string[] = [];
    client.on("message.created", async (evt) => {
      const tryIt = async (label: string, fn: () => Promise<unknown>) =>
        fn().then(
          () => results.push(`${label}:ok`),
          (e: { code: string }) => results.push(`${label}:${e.code}`),
        );
      await tryIt("thread", () => agent.chat.send({ channel: evt.channel.id, threadTs: evt.ts, markdown: "answer" }));
      await tryIt("top-level", () => agent.chat.send({ channel: evt.channel.id, markdown: "x" }));
      await tryIt("other-channel", () => agent.chat.send({ channel: "C0RANDOM001", threadTs: evt.ts, markdown: "x" }));
      await tryIt("delete", () => agent.chat.delete({ channel: evt.channel.id, ts: evt.ts }));
      await tryIt("search", () => agent.search.messages("secret"));
      await tryIt("history", () => agent.channels.getHistory({ channel: "C0RANDOM001" }));
      await tryIt("react", () => agent.reactions.add({ channel: evt.channel.id, ts: evt.ts, emoji: "eyes" }));
    });
    slack.socket.deliver(messageFrame());
    await until(() => results.length === 7);
    expect(results).toEqual([
      "thread:ok",
      "top-level:SCOPE_POLICY_VIOLATION",
      "other-channel:SCOPE_POLICY_VIOLATION",
      "delete:SCOPE_POLICY_VIOLATION",
      "search:SCOPE_POLICY_VIOLATION",
      "history:SCOPE_POLICY_VIOLATION",
      "react:ok",
    ]);
    expect(slack.callsTo("chat.postMessage")[0]!.fields.markdown_text).toBe("answer\n_ai_");
  });

  it("maps download overruns to SCOPE_POLICY_VIOLATION", async () => {
    const slack = new FakeSlack();
    slack.fileBodies.set("/files-pri/T-F1/big.log", { body: new Uint8Array(4096), contentType: "text/plain" });
    const { client } = await started(slack);
    const agent = client.scope({ read: { maxFileDownloadBytes: 1024 } });
    await expect(agent.files.downloadBuffer({ urlPrivate: "https://files.slack.com/files-pri/T-F1/big.log" })).rejects.toMatchObject({
      code: "SCOPE_POLICY_VIOLATION",
    });
    await expect(client.files.downloadBuffer({ urlPrivate: "https://files.slack.com/files-pri/T-F1/big.log" }, { maxBytes: 100 })).rejects.toMatchObject({
      code: "FILE_TOO_LARGE",
    });
    const ok = await client.files.downloadBuffer({ urlPrivate: "https://files.slack.com/files-pri/T-F1/big.log" });
    expect(ok.byteLength).toBe(4096);
  });
});

describe("gap recovery", () => {
  it("backfills channel history after a cold reconnect, in order, before buffered realtime frames", async () => {
    const slack = new FakeSlack();
    let resolveHistory: () => void = () => {};
    const historyGate = new Promise<void>((r) => (resolveHistory = r));
    slack.on("client.counts", () => ({ ok: true, channels: [{ id: "C0GENERAL01", latest: "1800000005.000100" }], mpims: [], ims: [] }));
    slack.on("conversations.history", async () => {
      await historyGate;
      return {
        ok: true,
        has_more: false,
        messages: [
          { type: "message", user: "U0BOB000001", text: "missed 2", ts: "1800000003.000100" },
          { type: "message", user: "U0BOB000001", text: "missed 1", ts: "1800000002.000100" },
        ],
      };
    });
    const { client } = await started(slack);
    const seen: Array<{ ts: string; delivery: string }> = [];
    const gaps: GabrielEvent[] = [];
    client.on("message.created", (e) => void seen.push({ ts: e.ts, delivery: e.delivery }));
    client.on("connection.gap", (e) => void gaps.push(e));
    slack.socket.deliver(messageFrame({ ts: "1800000001.000100" }));
    await until(() => seen.length === 1);
    await tick(20);

    client.core.ws.markReady(false);
    slack.socket.serverClose(1006); // no frt ticket → cold reconnect
    await until(() => slack.sockets.length === 2, 5_000);
    await until(() => slack.callsTo("conversations.history").length === 1);
    slack.socket.deliver(messageFrame({ ts: "1800000004.000100", text: "realtime during resync" }));
    await tick(20);
    resolveHistory();
    await until(() => seen.length === 4);
    expect(seen).toEqual([
      { ts: "1800000001.000100", delivery: "realtime" },
      { ts: "1800000002.000100", delivery: "backfill" },
      { ts: "1800000003.000100", delivery: "backfill" },
      { ts: "1800000004.000100", delivery: "realtime" },
    ]);
    expect(slack.callsTo("conversations.history")[0]!.fields.oldest).toBe("1800000001.000100");
    await until(() => gaps.length === 1);
    expect(gaps[0]).toMatchObject({ cause: "cold_resync", recovered: true, recoveredCount: 2, channelsRecovered: ["C0GENERAL01"] });
  });
});

describe("lifecycle", () => {
  it("stops gracefully and reports dead auth", async () => {
    const slack = new FakeSlack();
    const { client } = await started(slack);
    const dead: GabrielEvent[] = [];
    client.on("auth.dead", (e) => void dead.push(e));
    slack.token = "xoxc-rotated";
    slack.authAlive = false;
    await expect(client.users.getDnd("U0ALICE0001")).rejects.toMatchObject({ code: "AUTH_DEAD" });
    await until(() => dead.length === 1);
    expect(client.health().status).toBe("dead");
  });
});
