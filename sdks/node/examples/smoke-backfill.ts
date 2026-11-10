/**
 * Read-only live check of the backfill reads: direct conversation list, activity feed,
 * messages by timestamp and history fields. Opens no WebSocket and writes nothing; prints
 * counts and field presence only (no message content, names or IDs), and compares the unread
 * state before and after.
 *
 *   GABRIEL_COOKIE_D=… GABRIEL_WORKSPACE=your-workspace pnpm example examples/smoke-backfill.ts [days]
 */
import { createHash } from "node:crypto";
import { GabrielClient, loadSessionCookie, redactSecret } from "../src/index.js";

const session = loadSessionCookie();
if (!session) {
  console.error("No session: set GABRIEL_COOKIE_D or run `gabriel auth parse-curl --save`.");
  process.exit(2);
}
const days = Number(process.argv[2] ?? 30);
const since = `${Math.floor(Date.now() / 1000) - days * 86400}.000000`;
const ageDays = (ts: string) => ((Date.now() / 1000 - Number(ts)) / 86400).toFixed(1);
const tally = (values: string[]) => JSON.stringify(Object.fromEntries([...new Set(values)].map((v) => [v, values.filter((x) => x === v).length])));

const results: Array<[string, boolean, string]> = [];
const check = async (name: string, fn: () => Promise<string>) => {
  try {
    results.push([name, true, await fn()]);
  } catch (err) {
    const e = err as { code?: string; rawSlackError?: string; name?: string };
    results.push([name, false, `${e.code ?? e.name}${e.rawSlackError ? ` (${e.rawSlackError})` : ""}`]);
  }
};

const client = GabrielClient.fromCookie(session);
// Authenticate without opening the WebSocket: these reads only need the Web API.
await client.core.initialize();
const reader = client.scope({ read: { allowSearch: true }, write: { allowedActions: [] } });

/** Hash of every conversation's read cursor (new incoming messages do not move it), to prove nothing was marked. */
const unreadState = async () => {
  const res = await client.core.callWebApi<Record<string, unknown>>("client.counts", {});
  const rows = ["channels", "mpims", "ims"].flatMap((g) =>
    ((res[g] as Array<Record<string, unknown>> | undefined) ?? []).map((c) => `${String(c.id)}:${String(c.last_read)}`),
  );
  return { rows: rows.length, hash: createHash("sha256").update(rows.sort().join("\n")).digest("hex").slice(0, 12) };
};
const before = await unreadState();

let firstDm: string | undefined;
await check("channels.listDirect (first page)", async () => {
  const page = await reader.channels.listDirect();
  const items = page.items;
  firstDm = items.find((d) => d.channel.isIm)?.channel.id;
  return `items=${items.length} im=${items.filter((d) => d.channel.isIm).length} mpim=${items.filter((d) => d.channel.isMpim).length} withLatest=${items.filter((d) => d.latestTs).length} named=${items.filter((d) => d.channel.name).length} closed=${items.filter((d) => d.isOpen === false).length} next=${page.nextCursor ? "yes" : "no"}`;
});
await check(`channels.iterateDirect (active in ${days}d)`, async () => {
  let n = 0;
  let mpim = 0;
  for await (const d of reader.channels.iterateDirect({ activeSince: since })) {
    n++;
    if (d.channel.isMpim) mpim++;
  }
  return `conversations=${n} mpim=${mpim}`;
});
await check("channels.iterateHistory (one DM, window)", async () => {
  if (!firstDm) return "skipped (no 1:1 DM listed)";
  let n = 0;
  const subtypes: string[] = [];
  let threads = 0;
  let mentioning = 0;
  for await (const m of reader.channels.iterateHistory({ channel: firstDm, oldest: since })) {
    if (++n > 400) break;
    subtypes.push(m.subtype ?? "none");
    if (m.thread) threads++;
    if (m.mentionReasons.length) mentioning++;
  }
  return `messages=${n} subtypes=${tally(subtypes)} threaded=${threads} mentioningMe=${mentioning}`;
});

let sample: { channel: string; ts: string } | undefined;
await check("activity.list (at_user, first page)", async () => {
  const page = await reader.activity.list({ types: ["at_user"] });
  const items = page.items;
  const withMsg = items.filter((i) => i.message);
  const first = withMsg[0];
  if (first?.channel && first.ts) sample = { channel: first.channel.id, ts: first.ts };
  return `items=${items.length} types=${tally(items.map((i) => i.type))} withMessage=${withMsg.length} direct=${withMsg.filter((i) => i.message!.mentionReasons.includes("direct")).length} threaded=${items.filter((i) => i.threadTs).length} channelNamed=${items.filter((i) => i.channel?.name).length} next=${page.nextCursor ? "yes" : "no"}`;
});
await check("activity.list (limit 50, mixed types)", async () => {
  const page = await reader.activity.list({ types: ["at_user", "at_user_group", "at_channel", "at_everyone", "dm", "thread_v2"], limit: 50 });
  return `items=${page.items.length} types=${tally(page.items.map((i) => i.type))} withMessage=${page.items.filter((i) => i.message).length}`;
});
await check("activity.list (unreadOnly)", async () => `items=${(await reader.activity.list({ types: ["at_user"], unreadOnly: true })).items.length}`);
await check(`activity.iterate (at_user, ${days}d)`, async () => {
  let n = 0;
  let missing = 0;
  let oldest: string | undefined;
  let ordered = true;
  for await (const item of reader.activity.iterate({ types: ["at_user"], since, pageSize: 50 })) {
    n++;
    if (!item.message) missing++;
    if (oldest && Number(item.feedTs) > Number(oldest)) ordered = false;
    oldest = item.feedTs;
  }
  return `items=${n} withoutMessage=${missing} newestFirst=${ordered} oldestAgeDays=${oldest ? ageDays(oldest) : "-"}`;
});
await check("channels.getMessages (one activity message)", async () => {
  if (!sample) return "skipped (no activity message)";
  const got = await reader.channels.getMessages({ channel: sample.channel, timestamps: [sample.ts] });
  return `returned=${got.length} sameTs=${got[0]?.ts === sample.ts} markdown=${got[0]?.markdown ? "present" : "empty"}`;
});

const after = await unreadState();
results.push(["read cursors unchanged", before.hash === after.hash, `conversations=${before.rows}→${after.rows}`]);

console.log("\n== backfill read-only smoke results ==");
for (const [name, ok, detail] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${name} — ${detail}`);
console.log(`cookie used: ${redactSecret(session.cookieD)}`);
process.exit(results.every(([, ok]) => ok) ? 0 : 1);
