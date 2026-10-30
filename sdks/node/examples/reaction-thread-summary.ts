/**
 * React with :memo: on any message to get a summary of its whole thread.
 * Uses a least-privilege scoped view for every write.
 */
import { GabrielClient, loadSessionCookie } from "../src/index.js";

const session = loadSessionCookie();
if (!session) throw new Error("Set GABRIEL_COOKIE_D first.");
const client = GabrielClient.fromCookie(session);

client.on("interaction.reaction_added", async (evt, ctx) => {
  if (evt.user.id !== client.selfUserId || evt.reaction !== "memo" || !evt.target.ts) return;
  const rootTs = evt.target.threadRootTs ?? evt.target.ts;
  const agent = client.scope({
    read: { channels: [evt.channel.id], allowSearch: false },
    write: { targetScope: "trigger_thread_only", allowedActions: ["chat.createStream"], disclosureSuffix: "\n_— generated summary_" },
  });
  const thread = await agent.threads.getContext({ channel: evt.channel.id, threadTs: rootTs, limit: 200 });
  const stream = await agent.chat.createStream({ channel: evt.channel.id, threadTs: rootTs, initialMarkdown: "_summarizing…_" });
  // Replace with an LLM call over thread.formattedTranscript.
  stream.append(`**${thread.messages.length} messages** from ${thread.participants.map((p) => p.displayName ?? p.id).join(", ")}.`);
  if (!ctx.signal.aborted) await stream.finish();
});

await client.start();
