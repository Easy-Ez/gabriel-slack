/**
 * Quickstart: reply with a streamed answer whenever someone @-mentions you.
 *
 *   GABRIEL_COOKIE_D=xoxd-... GABRIEL_WORKSPACE=your-workspace pnpm example examples/mention-stream-reply.ts
 *
 * Replace `fakeLlm` with your own LLM client. Pass `signal` so generation stops
 * when the triggering message (or the streamed reply) is deleted.
 */
import { GabrielClient, consoleLogger, loadSessionCookie } from "../src/index.js";

async function* fakeLlm(prompt: string, signal: AbortSignal): AsyncGenerator<string> {
  const words = `You said:\n\n${prompt.slice(0, 400)}`.split(/(\s+)/);
  for (const w of words) {
    if (signal.aborted) return;
    await new Promise((r) => setTimeout(r, 30));
    yield w;
  }
}

const session = loadSessionCookie();
if (!session) throw new Error("Set GABRIEL_COOKIE_D or run `gabriel auth parse-curl --save` first.");

const client = GabrielClient.fromCookie({ ...session, logger: consoleLogger("info") });

client.onMessage({ mentionsMe: true }, async (evt, ctx) => {
  if (evt.origin !== "other_user") return; // ignore bots and your own messages
  const stream = await ctx.replyStream({ initialMarkdown: "_🤖 thinking…_", preserveUnreadCursor: true });
  const signal = AbortSignal.any([ctx.signal, stream.signal]);
  for await (const chunk of fakeLlm(evt.toLLMContext(), signal)) stream.append(chunk);
  if (!signal.aborted) await stream.finish();
});

client.on("auth.dead", (e) => console.error(`session expired for ${e.teamId}: ${e.reason}`));
client.onError((err, evt) => console.error("handler failed", evt?.eventId, err));

await client.start();
console.log(`listening as ${client.selfUserId} in ${client.teamId}`);
process.on("SIGINT", () => void client.stop({ drainMs: 5_000 }).then(() => process.exit(0)));
