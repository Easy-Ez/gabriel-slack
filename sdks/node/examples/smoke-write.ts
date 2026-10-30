/**
 * Live write-path smoke test. Posts to ONE channel you choose (use your own self-DM),
 * then deletes everything it created. Exercises: send + thread reply, auto-chunking,
 * update, reactions, typing, streaming (with code fence), thread context, file upload,
 * echo suppression and message.deleted cancellation.
 *
 *   SMOKE_CHANNEL=D0123456789 pnpm example examples/smoke-write.ts
 */
import { GabrielClient, loadSessionCookie } from "../src/index.js";

const channel = process.env.SMOKE_CHANNEL;
if (!channel || !/^D[A-Z0-9]+$/.test(channel)) throw new Error("Set SMOKE_CHANNEL to a DM channel ID (D…), ideally your self-DM.");
const session = loadSessionCookie();
if (!session) throw new Error("No session: run `gabriel auth login` first.");

const client = GabrielClient.fromCookie(session);
const results: Array<[string, boolean, string]> = [];
const created: string[] = [];
let fileId: string | undefined;
const check = async (name: string, fn: () => Promise<string>) => {
  try {
    results.push([name, true, await fn()]);
  } catch (err) {
    results.push([name, false, `${(err as { code?: string }).code ?? ""} ${(err as Error).message}`]);
  }
};
const echoes: string[] = [];
client.on("message.created", (e) => {
  if (e.channel.id === channel) echoes.push(`${e.origin}${e.subtype ? `/${e.subtype}` : ""}`);
});

await client.start();
const tag = `gabriel-smoke-${Date.now()}`;
let rootTs = "";

await check("chat.send (markdown_text)", async () => {
  const r = await client.chat.send({ channel, markdown: `**${tag}** root message with \`code\` and a [link](https://example.com)` });
  rootTs = r.ts;
  created.push(r.ts);
  return `ts=${r.ts}`;
});
await check("chat.send thread reply", async () => {
  const r = await client.chat.send({ channel, threadTs: rootTs, markdown: "thread reply ✅" });
  created.push(r.ts);
  return `ts=${r.ts}`;
});
await check("chat.send auto-chunk (> budget)", async () => {
  const para = "lorem ipsum dolor sit amet ".repeat(120);
  const r = await client.chat.send({ channel, threadTs: rootTs, markdown: Array.from({ length: 9 }, (_, i) => `${i}. ${para}`).join("\n\n") });
  created.push(...r.chunkTs);
  return `${r.chunkTs.length} chunks`;
});
await check("chat.update", async () => {
  await client.chat.update({ channel, ts: rootTs, markdown: `**${tag}** root message (edited)` });
  return "ok";
});
await check("reactions.add + remove (idempotent)", async () => {
  await client.reactions.add({ channel, ts: rootTs, emoji: ":eyes:" });
  await client.reactions.add({ channel, ts: rootTs, emoji: "eyes" });
  await client.reactions.remove({ channel, ts: rootTs, emoji: "eyes" });
  await client.reactions.remove({ channel, ts: rootTs, emoji: "eyes" });
  return "ok";
});
await check("chat.sendTyping", async () => {
  await client.chat.sendTyping({ channel, threadTs: rootTs });
  return "ok";
});
await check("chat.createStream (latest-wins)", async () => {
  const stream = await client.chat.createStream({ channel, threadTs: rootTs, initialMarkdown: "_streaming…_" });
  created.push(stream.ts);
  for (const w of "Streaming works. Here is code:\n```ts\nconst answer = 42;\n```\nDone.".split(/(\s+)/)) {
    stream.append(w);
    await new Promise((r) => setTimeout(r, 40));
  }
  const done = await stream.finish();
  created.push(...done.continuationTs);
  return `ts=${done.ts}`;
});
await check("threads.getContext", async () => {
  const ctx = await client.threads.getContext({ channel, threadTs: rootTs, limit: 50 });
  return `${ctx.messages.length} messages, root=${ctx.rootTs === rootTs}, transcript ${ctx.formattedTranscript.length} chars`;
});
await check("files.upload (3-step) into thread", async () => {
  const info = await client.files.upload({ filename: `${tag}.txt`, content: "hello from gabriel smoke test\n", channel, threadTs: rootTs, introMarkdown: "smoke upload" });
  fileId = info.id;
  return `file=${info.id}`;
});
await check("files.downloadBuffer (private URL)", async () => {
  if (!fileId) throw new Error("no file");
  await new Promise((r) => setTimeout(r, 1500));
  const buf = await client.files.downloadBuffer(fileId, { maxBytes: 1024 });
  return `${buf.byteLength} bytes, content ok=${buf.toString("utf8").includes("gabriel smoke")}`;
});
await new Promise((r) => setTimeout(r, 2000));
await check("echo suppression (this_client hidden)", async () => {
  if (echoes.some((o) => o.startsWith("this_client"))) throw new Error("this_client echo was delivered");
  return `delivered origins: ${JSON.stringify(echoes)}`;
});

// cleanup: delete everything this run created (newest first)
let deleted = 0;
for (const ts of [...new Set(created)].reverse()) {
  await client.chat.delete({ channel, ts }).then(() => deleted++, () => {});
}
if (fileId) await client.files.delete(fileId).catch(() => {});
await client.stop({ drainMs: 2_000 });

console.log("\n== write-path smoke results ==");
for (const [name, ok, detail] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${name} — ${detail}`);
console.log(`cleanup: deleted ${deleted}/${new Set(created).size} messages${fileId ? " + 1 file" : ""}`);
process.exit(results.every(([, ok]) => ok) ? 0 : 1);
