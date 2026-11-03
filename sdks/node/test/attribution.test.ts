import { afterEach, describe, expect, it } from "vitest";
import type { GabrielClient } from "../src/client/client.js";
import type { MessageCreatedEvent, MessageUpdatedEvent } from "../src/events/types.js";
import {
  markdownToPlainText,
  parseAttribution,
  renderAttribution,
  resolveAttribution,
  validateAttribution,
  type Attribution,
} from "../src/markdown/attribution.js";
import { measureMarkdown } from "../src/markdown/chunker.js";
import { FakeSlack, SELF, makeClient, messageFrame, tick, until } from "./harness.js";

const WHERE = { selfUserId: "U0ALICE0001", channel: "C0GENERAL01" };
const PAIMON = { name: "Paimon", role: "Assistant" } as const;

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

function blocksOf(fields: Record<string, string>): Array<Record<string, unknown>> {
  return JSON.parse(fields.blocks!) as Array<Record<string, unknown>>;
}

describe("renderAttribution", () => {
  it.each([
    ["confirmed", ":robot_face: Paimon (Assistant) · confirmed by <@U0ALICE0001>"],
    ["unconfirmed", ":robot_face: Paimon (Assistant) · sent for <@U0ALICE0001> · not reviewed"],
    ["hidden", ":robot_face: Paimon (Assistant)"],
  ] as const)("renders review %s", (review, text) => {
    const r = renderAttribution({ ...PAIMON, review }, WHERE);
    expect(r.text).toBe(text);
    expect(r.block).toEqual({ type: "context", block_id: `gabriel.attribution.${review}`, elements: [{ type: "mrkdwn", text }] });
  });

  it("defaults to unconfirmed and the robot icon; null icon and no role drop their parts", () => {
    expect(renderAttribution({ name: "Paimon" }, WHERE).text).toBe(":robot_face: Paimon · sent for <@U0ALICE0001> · not reviewed");
    expect(renderAttribution({ name: "Paimon", icon: null, review: "hidden" }, WHERE).text).toBe("Paimon");
    expect(renderAttribution({ name: "Paimon", icon: ":sparkles:", review: "hidden" }, WHERE).text).toBe(":sparkles: Paimon");
  });

  it("escapes and trims preset fields", () => {
    expect(renderAttribution({ name: "  <!channel> & co ", role: "a>b", review: "hidden" }, WHERE).text).toBe(":robot_face: &lt;!channel&gt; &amp; co (a&gt;b)");
  });

  it("writes custom text as given, with {self} and a function", () => {
    const r = renderAttribution({ text: ":sparkles: drafted by *Paimon*, reviewed by {self}" }, WHERE);
    expect(r.text).toBe(":sparkles: drafted by *Paimon*, reviewed by <@U0ALICE0001>");
    expect(r.block.block_id).toBe("gabriel.attribution.custom");
    const seen: unknown[] = [];
    const fn = renderAttribution(
      {
        text: (info) => {
          seen.push(info);
          return `via Paimon in ${info.channel} (${info.review})`;
        },
        review: "confirmed",
      },
      { ...WHERE, threadTs: "1800000000.000100" },
    );
    expect(fn.text).toBe("via Paimon in C0GENERAL01 (confirmed)");
    expect(seen).toEqual([{ selfUserId: "U0ALICE0001", channel: "C0GENERAL01", threadTs: "1800000000.000100", review: "confirmed" }]);
  });

  it.each([
    [{ name: "" }, /name/],
    [{ name: "two\nlines" }, /single line/],
    [{ name: "x".repeat(65) }, /64/],
    [{ name: "Paimon", role: "r".repeat(65) }, /64/],
    [{ name: "Paimon", icon: "robot" }, /icon/],
    [{ name: "Paimon", review: "maybe" }, /review/],
    [{ text: "" }, /empty/],
    [{ text: "hey <!here>" }, /@channel/],
    [{ text: "hey <!subteam^S0ONCALL001>" }, /user group/],
    [{ text: "\u{1F389}".repeat(160) }, /3000|3,000/],
  ] as Array<[unknown, RegExp]>)("rejects %#", (a, msg) => {
    expect(() => renderAttribution(a as Attribution, WHERE)).toThrow(msg);
    try {
      renderAttribution(a as Attribution, WHERE);
    } catch (err) {
      expect((err as { code?: string }).code).toBe("INVALID_ARGUMENT");
    }
  });

  it("rejects a broadcast mention returned by a text function", () => {
    expect(() => renderAttribution({ text: () => "<!everyone>" }, WHERE)).toThrow(/@channel/);
  });

  it("accepts confirmed per write but not as the client default", () => {
    expect(() => validateAttribution({ ...PAIMON, review: "confirmed" }, "write")).not.toThrow();
    expect(() => validateAttribution({ ...PAIMON, review: "confirmed" }, "client")).toThrow(/per write/);
  });
});

describe("resolveAttribution", () => {
  it("merges a partial preset over the default, and false turns it off", () => {
    expect(resolveAttribution(PAIMON, { review: "confirmed" })).toEqual({ ...PAIMON, review: "confirmed" });
    expect(resolveAttribution(PAIMON, undefined)).toEqual(PAIMON);
    expect(resolveAttribution(PAIMON, false)).toBeNull();
    expect(resolveAttribution(undefined, undefined)).toBeNull();
  });

  it("keeps a custom default when only review changes, and lets a write replace it", () => {
    const custom = { text: "via Paimon" };
    expect(resolveAttribution(custom, { review: "confirmed" })).toEqual({ text: "via Paimon", review: "confirmed" });
    expect(resolveAttribution(custom, { name: "Other" })).toEqual({ name: "Other" });
    expect(resolveAttribution(PAIMON, { text: "x" })).toEqual({ text: "x" });
  });

  it("requires a name when there is no default", () => {
    expect(() => resolveAttribution(undefined, { review: "confirmed" })).toThrow(/name/);
  });
});

describe("parseAttribution / markdownToPlainText", () => {
  it("parses the leading block only", () => {
    const block = renderAttribution({ ...PAIMON, review: "confirmed" }, WHERE).block;
    expect(parseAttribution([block, { type: "section", text: { type: "mrkdwn", text: "hi" } }], { userName: () => "Alice" })).toEqual({
      text: ":robot_face: Paimon (Assistant) · confirmed by @Alice",
      review: "confirmed",
    });
    expect(parseAttribution([{ type: "section", text: { type: "mrkdwn", text: "hi" } }, block])).toBeUndefined();
    expect(parseAttribution([{ ...block, block_id: "gabriel.attribution" }])).toEqual({ text: expect.any(String) });
    expect(parseAttribution([{ ...block, block_id: "gabriel.attributions" }])).toBeUndefined();
  });

  it("strips Markdown syntax for the text fallback", () => {
    expect(markdownToPlainText("# Title\n\n**bold** and [link](https://example.com)\n\n```ts\nconst x = 1;\n```")).toBe(
      "Title\n\nbold and link\n\nconst x = 1;",
    );
    expect(Array.from(markdownToPlainText("a".repeat(5000))).length).toBe(3000);
  });
});

describe("client attribution", () => {
  it("rejects an invalid default at construction", () => {
    expect(() => makeClient(new FakeSlack(), { attribution: { ...PAIMON, review: "confirmed" } as never })).toThrow(/per write/);
    expect(() => makeClient(new FakeSlack(), { attribution: { name: "" } })).toThrow(/name/);
  });

  it("sends [attribution, markdown] blocks with a text fallback instead of markdown_text", async () => {
    const { slack, client } = await started(new FakeSlack(), { attribution: PAIMON });
    await client.chat.send({ channel: "C0GENERAL01", markdown: "**done**" });
    const f = slack.callsTo("chat.postMessage")[0]!.fields;
    expect(f.markdown_text).toBeUndefined();
    expect(f.text).toBe("done");
    expect(blocksOf(f)).toEqual([
      { type: "context", block_id: "gabriel.attribution.unconfirmed", elements: [{ type: "mrkdwn", text: `:robot_face: Paimon (Assistant) · sent for <@${SELF}> · not reviewed` }] },
      { type: "markdown", text: "**done**" },
    ]);
  });

  it("applies per-write overrides and false", async () => {
    const { slack, client } = await started(new FakeSlack(), { attribution: PAIMON });
    await client.chat.send({ channel: "C0GENERAL01", markdown: "a", attribution: { review: "confirmed" } });
    await client.chat.send({ channel: "C0GENERAL01", markdown: "b", attribution: false });
    const [a, b] = slack.callsTo("chat.postMessage").map((c) => c.fields);
    expect(blocksOf(a!)[0]!.block_id).toBe("gabriel.attribution.confirmed");
    expect(b!.markdown_text).toBe("b");
    expect(b!.blocks).toBeUndefined();
  });

  it("leaves writes unchanged without a default", async () => {
    const { slack, client } = await started();
    await client.chat.send({ channel: "C0GENERAL01", markdown: "plain" });
    expect(slack.callsTo("chat.postMessage")[0]!.fields.markdown_text).toBe("plain");
    expect(client.chat.previewAttribution()).toBeNull();
    const one = await client.chat.send({ channel: "C0GENERAL01", markdown: "x", attribution: { ...PAIMON, review: "hidden" } });
    expect(one.ts).toBeDefined();
    expect(blocksOf(slack.callsTo("chat.postMessage")[1]!.fields)[0]!.block_id).toBe("gabriel.attribution.hidden");
  });

  it("prepends the attribution block to caller blocks", async () => {
    const { slack, client } = await started(new FakeSlack(), { attribution: PAIMON });
    const body = { type: "section", text: { type: "mrkdwn", text: "hi" } };
    await client.chat.send({ channel: "C0GENERAL01", blocks: [body] });
    const blocks = blocksOf(slack.callsTo("chat.postMessage")[0]!.fields);
    expect(blocks.map((b) => b.type)).toEqual(["context", "section"]);
  });

  it("carries the block on every auto-chunked message within the reduced budget", async () => {
    const { slack, client } = await started(new FakeSlack(), { attribution: PAIMON });
    const paragraph = "word ".repeat(400).trim();
    const res = await client.chat.send({ channel: "C0GENERAL01", markdown: Array.from({ length: 12 }, () => paragraph).join("\n\n") });
    expect(res.chunkTs.length).toBeGreaterThan(1);
    for (const call of slack.callsTo("chat.postMessage")) {
      const [attr, body] = blocksOf(call.fields);
      expect(attr!.block_id).toBe("gabriel.attribution.unconfirmed");
      const total = measureMarkdown(String((attr!.elements as Array<{ text: string }>)[0]!.text)).effective + measureMarkdown(String(body!.text)).effective;
      expect(total).toBeLessThanOrEqual(11_500);
    }
  });

  it("keeps the block on chat.update", async () => {
    const { slack, client } = await started(new FakeSlack(), { attribution: PAIMON });
    const { ts } = await client.chat.send({ channel: "C0GENERAL01", markdown: "v1" });
    await client.chat.update({ channel: "C0GENERAL01", ts, markdown: "v2", attribution: { review: "confirmed" } });
    const f = slack.callsTo("chat.update")[0]!.fields;
    expect(f.markdown_text).toBeUndefined();
    expect(blocksOf(f).map((b) => b.block_id ?? b.type)).toEqual(["gabriel.attribution.confirmed", "markdown"]);
    expect(f.text).toBe("v2");
  });

  it("carries the block on the stream placeholder and every refresh", async () => {
    const slack = new FakeSlack();
    slack.on("chat.update", (f) => ({ ok: true, ts: f.ts, channel: f.channel }));
    const { client } = await started(slack, { attribution: PAIMON, chat: { streamUpdatesPerSecond: 100, streamMinIntervalMs: 0 } });
    const stream = await client.chat.createStream({ channel: "C0GENERAL01" });
    stream.append("hello ");
    await tick(5);
    stream.append("world");
    await stream.finish();
    const writes = [...slack.callsTo("chat.postMessage"), ...slack.callsTo("chat.update")];
    expect(writes.length).toBeGreaterThanOrEqual(2);
    for (const w of writes) expect(blocksOf(w.fields)[0]!.block_id).toBe("gabriel.attribution.unconfirmed");
    expect(blocksOf(slack.callsTo("chat.update").at(-1)!.fields)[1]).toEqual({ type: "markdown", text: "hello world" });
  });

  it("passes ctx.reply overrides through", async () => {
    const { slack, client } = await started(new FakeSlack(), { attribution: PAIMON });
    client.on("message.created", async (_evt, ctx) => {
      await ctx.reply({ markdown: "thanks", attribution: { review: "confirmed" } });
    });
    slack.socket.deliver(messageFrame({ text: "hi" }));
    await until(() => slack.callsTo("chat.postMessage").length === 1);
    expect(blocksOf(slack.callsTo("chat.postMessage")[0]!.fields)[0]!.block_id).toBe("gabriel.attribution.confirmed");
  });

  it("previews the exact line without sending", async () => {
    const { slack, client } = await started(new FakeSlack(), { attribution: PAIMON });
    expect(client.chat.previewAttribution({ review: "confirmed" })!.text).toBe(`:robot_face: Paimon (Assistant) · confirmed by <@${SELF}>`);
    expect(client.chat.previewAttribution(false)).toBeNull();
    expect(slack.callsTo("chat.postMessage")).toHaveLength(0);
  });

  it("denies attribution overrides in a scoped view but applies the default", async () => {
    const { slack, client } = await started(new FakeSlack(), { attribution: PAIMON });
    const agent = client.scope({});
    await expect(agent.chat.send({ channel: "C0GENERAL01", markdown: "x", attribution: { review: "confirmed" } })).rejects.toMatchObject({
      code: "SCOPE_POLICY_VIOLATION",
    });
    await expect(agent.chat.send({ channel: "C0GENERAL01", markdown: "x", attribution: false })).rejects.toMatchObject({ code: "SCOPE_POLICY_VIOLATION" });
    await agent.chat.send({ channel: "C0GENERAL01", markdown: "y" });
    expect(blocksOf(slack.callsTo("chat.postMessage")[0]!.fields)[0]!.block_id).toBe("gabriel.attribution.unconfirmed");
  });
});

describe("received attribution", () => {
  const line = { type: "context", block_id: "gabriel.attribution.confirmed", elements: [{ type: "mrkdwn", text: ":robot_face: Paimon · confirmed by <@U0ALICE0001>" }] };
  const body = (text: string) => ({ type: "rich_text", elements: [{ type: "rich_text_section", elements: [{ type: "text", text }] }] });

  it("strips the line from markdown and exposes it on message.created and LLM context", async () => {
    const { slack, client } = await started();
    const got: MessageCreatedEvent[] = [];
    client.on("message.created", (e) => void got.push(e));
    slack.socket.deliver(messageFrame({ text: "see you", blocks: [line, body("see you")] }));
    await until(() => got.length === 1);
    expect(got[0]!.markdown).toBe("see you");
    expect(got[0]!.attribution).toEqual({ text: ":robot_face: Paimon · confirmed by @Alice", review: "confirmed" });
    expect(got[0]!.toLLMContext()).toContain("attribution: :robot_face: Paimon · confirmed by @Alice (self-declared by the sender, unverified)");
  });

  it("exposes the current line on message.updated", async () => {
    const { slack, client } = await started();
    const got: MessageUpdatedEvent[] = [];
    client.on("message.updated", (e) => void got.push(e));
    slack.socket.deliver({
      type: "message",
      subtype: "message_changed",
      channel: "C0GENERAL01",
      ts: "1800000002.000200",
      event_ts: "1800000002.000200",
      message: { type: "message", user: "U0ALICE0001", ts: "1800000001.000100", text: "v2", blocks: [line, body("v2")], edited: { ts: "1800000002.000200" } },
      previous_message: { type: "message", user: "U0ALICE0001", ts: "1800000001.000100", text: "v1", blocks: [line, body("v1")] },
    });
    await until(() => got.length === 1);
    expect(got[0]!.currentMarkdown).toBe("v2");
    expect(got[0]!.previousMarkdown).toBe("v1");
    expect(got[0]!.attribution?.review).toBe("confirmed");
  });

  it("marks attributed messages in thread context", async () => {
    const slack = new FakeSlack();
    slack.on("conversations.replies", () => ({
      ok: true,
      has_more: false,
      messages: [
        { type: "message", user: "U0ALICE0001", ts: "1800000001.000100", thread_ts: "1800000001.000100", text: "question" },
        { type: "message", user: "U0BOB000001", ts: "1800000001.000200", thread_ts: "1800000001.000100", text: "answer", blocks: [line, body("answer")] },
      ],
    }));
    const { client } = await started(slack);
    const ctx = await client.threads.getContext({ channel: "C0GENERAL01", threadTs: "1800000001.000100" });
    expect(ctx.messages[1]!.markdown).toBe("answer");
    expect(ctx.messages[1]!.attribution).toEqual({ text: ":robot_face: Paimon · confirmed by @Alice", review: "confirmed" });
    expect(ctx.formattedTranscript).toContain("Bob (@bob) [attribution: :robot_face: Paimon · confirmed by @Alice]:");
  });
});
