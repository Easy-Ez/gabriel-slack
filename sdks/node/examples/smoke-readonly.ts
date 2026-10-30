/**
 * Read-only live smoke test: no messages, reactions, marks or uploads are written.
 * Verifies cookie exchange, WebSocket handshake, hydration, presence subscription,
 * EdgeAPI / Web API reads, then listens passively and reports event types (no content).
 *
 *   GABRIEL_COOKIE_D=… GABRIEL_WORKSPACE=your-workspace pnpm example examples/smoke-readonly.ts [listenSeconds]
 */
import { GabrielClient, loadSessionCookie, redactSecret, type GabrielEvent } from "../src/index.js";

const session = loadSessionCookie();
if (!session) {
  console.error("No session: set GABRIEL_COOKIE_D or run `gabriel auth parse-curl --save`.");
  process.exit(2);
}
const listenMs = Number(process.argv[2] ?? 30) * 1000;
const results: Array<[string, boolean, string]> = [];
const check = async (name: string, fn: () => Promise<string>) => {
  try {
    results.push([name, true, await fn()]);
  } catch (err) {
    results.push([name, false, err instanceof Error ? `${(err as { code?: string }).code ?? err.name}: ${err.message}` : String(err)]);
  }
};

const client = GabrielClient.fromCookie({ ...session, loopGuard: { includeSelfMessages: true } });
const counts = new Map<string, number>();
const seen: GabrielEvent[] = [];
const it = client.events();
void (async () => {
  for await (const evt of it) {
    counts.set(evt.type, (counts.get(evt.type) ?? 0) + 1);
    seen.push(evt);
  }
})();

await check("start (cookie exchange + WebSocket hello)", async () => {
  await client.start();
  return `team=${client.teamId} self=${client.selfUserId} ws=${client.health().wsState}`;
});
await check("users.resolve(self) via EdgeAPI", async () => {
  const me = await client.users.resolve(client.selfUserId);
  return me ? `resolved (displayName ${me.displayName ? "present" : "missing"})` : "not found";
});
await check("users.getDnd(self) via dnd.teamInfo", async () => JSON.stringify(Object.keys(await client.users.getDnd(client.selfUserId))));
await check("client.counts via core.callWebApi", async () => {
  const res = await client.core.callWebApi<{ channels?: unknown[]; ims?: unknown[] }>("client.counts", {});
  return `channels=${res.channels?.length ?? 0} ims=${res.ims?.length ?? 0}`;
});
await check("presence.watch(self) initial snapshot", async () => {
  const got = new Promise<string>((resolve, reject) => {
    const off = client.on("presence.changed", (e) => {
      if (e.user.id === client.selfUserId) {
        off();
        resolve(`presence=${e.presence} initial=${e.isInitialSnapshot}`);
      }
    });
    setTimeout(() => reject(new Error("no snapshot within 10s")), 10_000);
  });
  await client.presence.watch([client.selfUserId]);
  return got;
});

console.log(`listening passively for ${listenMs / 1000}s…`);
await new Promise((r) => setTimeout(r, listenMs));
const health = client.health();
await client.stop({ drainMs: 2_000 });
await it.return?.();

console.log("\n== read-only smoke results ==");
for (const [name, ok, detail] of results) console.log(`${ok ? "PASS" : "FAIL"}  ${name} — ${detail}`);
console.log(`health before stop: ${health.status} ws=${health.wsState} lastFrameAgeMs=${health.lastFrameAgeMs}`);
console.log(`events observed: ${JSON.stringify(Object.fromEntries(counts))}`);
const drift = seen.flatMap((e) => (e.type === "guard.protocol_drift" ? [`${e.unknownType}${e.unknownSubtype ? `/${e.unknownSubtype}` : ""}`] : []));
if (drift.length) console.log(`unknown frame types (protocol drift): ${drift.join(", ")}`);
const msg = seen.find((e) => e.type === "message.created");
if (msg && msg.type === "message.created") {
  console.log(`sample message.created shape: sender.displayName=${msg.sender.displayName ? "set" : "unset"} channel.name=${msg.channel.name ? "set" : "unset"} origin=${msg.origin} mentions=${msg.mentions.length}`);
}
console.log(`cookie used: ${redactSecret(session.cookieD)}`);
process.exit(results.every(([, ok]) => ok) ? 0 : 1);
