/**
 * Live send + receive round trip in one channel, then cleanup.
 *
 * Send: everything except one root message goes into that root's thread (chunking,
 * update, reactions, typing, streaming, upload/download, thread context).
 * Receive: for LISTEN_SECONDS, every message you post from another client (or anyone
 * posts inside the test thread), every edit and every reaction gets a receipt reply in
 * that message's thread: event type, sender, origin, a short excerpt and delivery latency.
 * Finally every message / file this script created is deleted (your own messages stay).
 *
 *   LIVE_CHANNEL=C0123456789 LISTEN_SECONDS=240 pnpm example examples/live-roundtrip.ts
 */
import { GabrielClient, loadSessionCookie, type MessageCreatedEvent } from "../src/index.js";

const channel = process.env.LIVE_CHANNEL;
if (!channel || !/^[CGD][A-Z0-9]+$/.test(channel)) throw new Error("Set LIVE_CHANNEL to a channel ID.");
const listenMs = Number(process.env.LISTEN_SECONDS ?? 240) * 1000;
const session = loadSessionCookie();
if (!session) throw new Error("No session: run `gabriel auth login` first.");

const client = GabrielClient.fromCookie({ ...session, loopGuard: { maxAutoRepliesPerMinute: 30 } });
const created = new Set<string>();
let fileId: string | undefined;
const results: Array<[string, boolean, string]> = [];
const receipts: string[] = [];
const log = (line: string) => console.log(`[${new Date().toISOString().slice(11, 19)}] ${line}`);
const check = async (name: string, fn: () => Promise<string>) => {
  try {
    const detail = await fn();
    results.push([name, true, detail]);
    log(`PASS ${name} — ${detail}`);
  } catch (err) {
    results.push([name, false, `${(err as { code?: string }).code ?? ""} ${(err as Error).message}`]);
    log(`FAIL ${name} — ${(err as Error).message}`);
  }
};
const latency = (ts: string) => `${Math.max(0, Date.now() - Math.round(Number(ts) * 1000))}ms`;
const excerpt = (md: string) => md.replace(/\s+/g, " ").slice(0, 80);

await client.start();
log(`connected as ${client.selfUserId}`);
const tag = new Date().toISOString().slice(0, 19).replace("T", " ");
let rootTs = "";

// ---------------------------------------------------------------- send paths
await check("chat.send root", async () => {
  const r = await client.chat.send({ channel, markdown: `🧪 *Gabriel SDK 收发测试* (${tag}) — 测试结束后本线程内容会自动删除` });
  rootTs = r.ts;
  created.add(r.ts);
  return `ts=${r.ts}`;
});
await check("thread reply + auto-chunk", async () => {
  const para = "chunking check ".repeat(160);
  const r = await client.chat.send({ channel, threadTs: rootTs, markdown: Array.from({ length: 8 }, (_, i) => `**${i}.** ${para}`).join("\n\n") });
  r.chunkTs.forEach((t) => created.add(t));
  return `${r.chunkTs.length} chunks`;
});
await check("chat.update", async () => {
  await client.chat.update({ channel, ts: rootTs, markdown: `🧪 *Gabriel SDK 收发测试* (${tag}) — 已编辑 ✏️ 测试结束后本线程内容会自动删除` });
  return "ok";
});
await check("reactions add/remove", async () => {
  await client.reactions.add({ channel, ts: rootTs, emoji: "test_tube" });
  await client.reactions.add({ channel, ts: rootTs, emoji: "test_tube" });
  await client.reactions.remove({ channel, ts: rootTs, emoji: "test_tube" });
  return "idempotent ok";
});
await check("typing indicator", async () => {
  await client.chat.sendTyping({ channel, threadTs: rootTs });
  return "sent";
});
await check("streaming reply", async () => {
  const stream = await client.chat.createStream({ channel, threadTs: rootTs, initialMarkdown: "_streaming…_" });
  created.add(stream.ts);
  for (const w of "流式回复验证：逐词刷新，代码块实时闭合\n```ts\nconst ok = true;\n```\n完成 ✅".split(/(\s+)/)) {
    stream.append(w);
    await new Promise((r) => setTimeout(r, 60));
  }
  const done = await stream.finish();
  done.continuationTs.forEach((t) => created.add(t));
  return `ts=${done.ts}`;
});
await check("file upload + private download", async () => {
  const info = await client.files.upload({ filename: "gabriel-roundtrip.txt", content: "gabriel round trip\n", channel, threadTs: rootTs });
  fileId = info.id;
  await new Promise((r) => setTimeout(r, 1500));
  const buf = await client.files.downloadBuffer(info.id, { maxBytes: 1024 });
  return `file ${info.id}, downloaded ${buf.byteLength} bytes, match=${buf.toString("utf8").includes("round trip")}`;
});
await check("thread context", async () => {
  const ctx = await client.threads.getContext({ channel, threadTs: rootTs, limit: 50 });
  return `${ctx.messages.length} messages, participants=${ctx.participants.length}`;
});

// ---------------------------------------------------------------- receive paths
const receipt = async (threadTs: string, text: string) => {
  const r = await client.chat.send({ channel, threadTs, markdown: text });
  r.chunkTs.forEach((t) => created.add(t));
};
const inScope = (evt: MessageCreatedEvent) => evt.origin === "self_other_client" || evt.threadTs === rootTs;

client.on("message.created", async (evt) => {
  if (evt.channel.id !== channel || !inScope(evt)) return;
  const line = `📥 收到 \`message.created\` · 来自 ${evt.sender.displayName ?? evt.sender.id} · origin=\`${evt.origin}\`${evt.isThreadReply ? " · 线程回复" : ""}${evt.files.length ? ` · 附件 ${evt.files.length}` : ""} · 延迟 ${latency(evt.ts)}\n> ${excerpt(evt.markdown) || "(空)"}`;
  receipts.push(`message.created ${evt.origin} ${latency(evt.ts)}`);
  log(`RECV message.created origin=${evt.origin} latency=${latency(evt.ts)}`);
  await receipt(evt.threadTs ?? evt.ts, line);
});
client.on("message.updated", async (evt) => {
  if (evt.channel.id !== channel || evt.origin !== "self_other_client") return;
  receipts.push(`message.updated ${evt.origin}`);
  log("RECV message.updated");
  await receipt(evt.threadTs ?? evt.ts, `✏️ 收到 \`message.updated\` · 编辑后：${excerpt(evt.currentMarkdown)}\n> 编辑前：${excerpt(evt.previousMarkdown ?? "")}`);
});
client.on("interaction.reaction_added", async (evt) => {
  if (evt.channel.id !== channel || evt.user.id !== client.selfUserId || !evt.target.ts || created.has(evt.target.ts) && evt.reaction === "test_tube") return;
  receipts.push(`reaction_added :${evt.reaction}:`);
  log(`RECV reaction_added :${evt.reaction}:`);
  await receipt(evt.target.threadRootTs ?? evt.target.ts, `😀 收到 \`interaction.reaction_added\` :${evt.reaction}: · 目标 ts=${evt.target.ts}`);
});
client.on("message.deleted", (evt) => {
  if (evt.channel.id === channel && !created.has(evt.deletedTs)) {
    receipts.push("message.deleted");
    log(`RECV message.deleted ${evt.deletedTs}`);
  }
});

await receipt(rootTs, `👂 开始监听 ${Math.round(listenMs / 1000)} 秒：请你在本频道（或本线程）发消息、编辑消息、加表情，我会在对应消息的线程里回执。`);
log(`LISTENING ${Math.round(listenMs / 1000)}s — post / edit / react now`);
await new Promise((r) => setTimeout(r, listenMs));

// ---------------------------------------------------------------- cleanup
log("cleaning up…");
let deleted = 0;
for (const ts of [...created].sort().reverse()) {
  await client.chat.delete({ channel, ts }).then(
    () => deleted++,
    (e) => log(`delete ${ts} failed: ${(e as Error).message}`),
  );
}
if (fileId) await client.files.delete(fileId).catch(() => {});
await client.stop({ drainMs: 3_000 });

console.log("\n== round trip results ==");
for (const [name, ok, detail] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${name} — ${detail}`);
console.log(`receipts (${receipts.length}): ${receipts.join(" | ") || "none"}`);
console.log(`cleanup: deleted ${deleted}/${created.size} messages${fileId ? " + 1 file" : ""}`);
process.exit(results.every(([, ok]) => ok) ? 0 : 1);
