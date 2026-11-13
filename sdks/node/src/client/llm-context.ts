import type { MessageCreatedEvent } from "../events/types.js";

/** Neutralizes tag-like sequences that could close the untrusted-content envelope. */
function neutralize(text: string): string {
  return text.replace(/<(\/?)(slack_message|content|meta|link_previews|link_preview)\b/gi, "&lt;$1$2");
}

function oneLine(text: string): string {
  return neutralize(text).replace(/[\r\n]+/g, " ").slice(0, 200);
}

/**
 * Renders a message as an LLM-ready context block: metadata is separated from the
 * body, and the body is explicitly marked untrusted. Link previews follow the body in a
 * `<link_previews>` section labelled as generated automatically, not written by the sender.
 * Tell the model in the system prompt that text inside `<content>` and `<link_previews>` is
 * user data, not instructions.
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
  if (evt.attribution) lines.push(`    attribution: ${oneLine(evt.attribution.text)} (self-declared by the sender, unverified)`);
  lines.push(`    mentions_me: ${evt.isMentioningMe}`);
  if (evt.files.length) lines.push(`    files: ${evt.files.map((f) => `${oneLine(f.name)} (${f.mimeType})`).join(", ")}`);
  lines.push(`  </meta>`, `  <content>`);
  for (const l of neutralize(evt.markdown || evt.text).split("\n")) lines.push(`  ${l}`);
  lines.push(`  </content>`);
  const previews = evt.linkPreviews ?? [];
  if (previews.length) {
    lines.push(`  <link_previews note="Generated automatically by Slack or an app from links in the message; not written by the sender.">`);
    for (const p of previews) {
      lines.push(`    <link_preview>`, `      url: ${oneLine(p.url)}`);
      if (p.serviceName) lines.push(`      service: ${oneLine(p.serviceName)}`);
      if (p.title) lines.push(`      title: ${oneLine(p.title)}`);
      if (p.text) {
        lines.push(`      text:`);
        for (const l of neutralize(p.text).split("\n")) lines.push(`        ${l}`);
      }
      lines.push(`    </link_preview>`);
    }
    lines.push(`  </link_previews>`);
  }
  lines.push(`</slack_message>`);
  return lines.join("\n");
}

/** Attaches `toLLMContext()` as a non-enumerable method (keeps JSON serialization clean). */
export function attachLLMContext(evt: MessageCreatedEvent): MessageCreatedEvent {
  Object.defineProperty(evt, "toLLMContext", { value: () => toLLMContext(evt), enumerable: false, configurable: true });
  return evt;
}
