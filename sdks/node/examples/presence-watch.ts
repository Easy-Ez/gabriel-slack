/**
 * Get a DM when colleagues come online and are not in Do-Not-Disturb.
 *
 *   WATCH=U0123456789,U0123456790 MY_DM=D0123456789 pnpm example examples/presence-watch.ts
 */
import { GabrielClient, loadSessionCookie } from "../src/index.js";

const session = loadSessionCookie();
if (!session) throw new Error("Set GABRIEL_COOKIE_D first.");
const watch = (process.env.WATCH ?? "").split(",").filter(Boolean);
const myDm = process.env.MY_DM;
if (watch.length === 0 || !myDm) throw new Error("Set WATCH (user IDs) and MY_DM (your self-DM channel ID).");

const client = GabrielClient.fromCookie(session);
await client.start();
const handle = await client.presence.watch(watch, { trackProfileAndDnd: true });

client.on("presence.changed", async (evt) => {
  if (evt.isInitialSnapshot || evt.presence !== "active") return;
  const { dnd, status } = await client.presence.getStatus(evt.user.id);
  const now = Date.now() / 1000;
  if (dnd?.enabled && now >= dnd.nextStartTs && now < dnd.nextEndTs) return;
  const note = status?.text ? ` (${status.emoji} ${status.text})` : "";
  await client.chat.send({ channel: myDm, markdown: `👋 <@${evt.user.id}> is online${note}` });
});

process.on("SIGINT", async () => {
  await handle.unsubscribe();
  await client.stop();
  process.exit(0);
});
