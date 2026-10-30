import type { MessageCreatedEvent } from "../events/types.js";

/** Neutralizes tag-like sequences that could close the untrusted-content envelope. */
function neutralize(text: string): string {
  return text.replace(/<(\/?)(slack_message|content|meta)\b/gi, "&lt;$1$2");
}

function oneLine(text: string): string {
  return neutralize(text).replace(/[\r\n]+/g, " ").slice(0, 200);
}

/**
 * Renders a message as an LLM-ready context block: metadata is separated from the
 * body, and the body is explicitly marked untrusted. Tell the model in the system
 * prompt that text inside `<content>` is user data, not instructions.
 */
export function toLLMContext(evt: MessageCreatedEvent): string {
  const ch = evt.channel;
  const kind = ch.isIm ? "direct message" : ch.isMpim ? "group DM" : ch.isPrivate ? "private" : ch.isPrivate === false ? "public" : "unknown";
  const chLabel = ch.isIm ? (ch.name ?? ch.id) : `#${ch.name ?? ch.id}`;
  const s = evt.sender;
  const senderName = s.displayName ?? s.name ?? s.id;
  const lines = [
    `<slack_message untrusted="true">`,
    `  <meta>`,
    `    channel: ${oneLine(chLabel)} (${ch.id}, ${kind})`,
    `    sender: ${oneLine(senderName)}${s.handle ? ` (@${oneLine(s.handle)}, ${s.id})` : ` (${s.id})`}${s.isBot ? " [bot]" : ""}`,
    `    sender_is_external: ${s.isExternal === true}`,
    `    ts: ${evt.ts}`,
  ];
  if (evt.thread) lines.push(`    thread_root_ts: ${evt.thread.rootTs}`);
  lines.push(`    mentions_me: ${evt.isMentioningMe}`);
  if (evt.files.length) lines.push(`    files: ${evt.files.map((f) => `${oneLine(f.name)} (${f.mimeType})`).join(", ")}`);
  lines.push(`  </meta>`, `  <content>`);
  for (const l of neutralize(evt.markdown || evt.text).split("\n")) lines.push(`  ${l}`);
  lines.push(`  </content>`, `</slack_message>`);
  return lines.join("\n");
}

/** Attaches `toLLMContext()` as a non-enumerable method (keeps JSON serialization clean). */
export function attachLLMContext(evt: MessageCreatedEvent): MessageCreatedEvent {
  Object.defineProperty(evt, "toLLMContext", { value: () => toLLMContext(evt), enumerable: false, configurable: true });
  return evt;
}
