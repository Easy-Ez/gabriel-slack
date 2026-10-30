/**
 * Converts Slack Block Kit `blocks` (rich_text and common layout blocks) and legacy
 * mrkdwn text into standard Markdown that LLMs consume well. Mentions resolve to
 * readable names through an optional resolver; unresolved mentions keep their
 * deterministic `<@U…>` / `<#C…>` form.
 */
export interface EntityResolver {
  userName?(userId: string): string | undefined;
  channelName?(channelId: string): string | undefined;
  usergroupHandle?(subteamId: string): string | undefined;
}

interface RichTextStyle {
  bold?: boolean;
  italic?: boolean;
  strike?: boolean;
  code?: boolean;
}

type Node = Record<string, unknown> & { type?: string; elements?: unknown[] };

const asNodes = (v: unknown): Node[] => (Array.isArray(v) ? (v.filter((x) => x && typeof x === "object") as Node[]) : []);
const str = (v: unknown): string => (typeof v === "string" ? v : "");

function longestRun(text: string, ch: string): number {
  let max = 0;
  let cur = 0;
  for (const c of text) {
    cur = c === ch ? cur + 1 : 0;
    if (cur > max) max = cur;
  }
  return max;
}

function codeSpan(text: string): string {
  const fence = "`".repeat(longestRun(text, "`") + 1);
  const pad = text.startsWith("`") || text.endsWith("`") ? " " : "";
  return `${fence}${pad}${text}${pad}${fence}`;
}

function applyStyle(text: string, style: RichTextStyle | undefined): string {
  if (!style || text.length === 0) return text;
  if (style.code) return codeSpan(text);
  // Keep surrounding whitespace outside the emphasis markers so Markdown parses it.
  const m = /^(\s*)([\s\S]*?)(\s*)$/.exec(text);
  const [, lead = "", core = "", trail = ""] = m ?? [];
  if (!core) return text;
  let out = core;
  if (style.strike) out = `~~${out}~~`;
  if (style.italic) out = `*${out}*`;
  if (style.bold) out = `**${out}**`;
  return `${lead}${out}${trail}`;
}

function inline(elements: unknown, r: EntityResolver): string {
  let out = "";
  for (const el of asNodes(elements)) {
    const style = el.style as RichTextStyle | undefined;
    switch (el.type) {
      case "text":
        out += applyStyle(str(el.text), style);
        break;
      case "user": {
        const id = str(el.user_id);
        const name = r.userName?.(id);
        out += applyStyle(name ? `@${name}` : `<@${id}>`, style);
        break;
      }
      case "usergroup": {
        const id = str(el.usergroup_id);
        const handle = r.usergroupHandle?.(id);
        out += applyStyle(handle ? `@${handle}` : `<!subteam^${id}>`, style);
        break;
      }
      case "channel": {
        const id = str(el.channel_id);
        const name = r.channelName?.(id);
        out += applyStyle(name ? `#${name}` : `<#${id}>`, style);
        break;
      }
      case "broadcast":
        out += applyStyle(`@${str(el.range) || "channel"}`, style);
        break;
      case "emoji": {
        const unicode = str(el.unicode);
        if (unicode && /^[0-9a-f-]+$/i.test(unicode)) {
          try {
            out += String.fromCodePoint(...unicode.split("-").map((h) => parseInt(h, 16)));
            break;
          } catch {
            /* fall through to shortcode */
          }
        }
        out += `:${str(el.name)}:`;
        break;
      }
      case "link": {
        const url = str(el.url).replace(/\)/g, "%29");
        const text = str(el.text);
        out += applyStyle(text && text !== el.url ? `[${text}](${url})` : url, style);
        break;
      }
      case "date":
        out += str(el.fallback) || str(el.timestamp);
        break;
      default:
        out += typeof el.text === "string" ? applyStyle(el.text, style) : str(el.fallback);
    }
  }
  return out;
}

function quotePrefix(text: string, depth: number): string {
  if (depth <= 0) return text;
  const prefix = "> ".repeat(depth);
  return text
    .split("\n")
    .map((l) => `${prefix}${l}`)
    .join("\n");
}

function preformatted(node: Node): string {
  let code = "";
  for (const el of asNodes(node.elements)) {
    if (el.type === "text") code += str(el.text);
    else if (el.type === "link") code += str(el.text) || str(el.url);
    else if (el.type === "user") code += `<@${str(el.user_id)}>`;
    else if (el.type === "channel") code += `<#${str(el.channel_id)}>`;
    else if (el.type === "emoji") code += `:${str(el.name)}:`;
  }
  code = code.replace(/\n+$/, "");
  const fence = "`".repeat(Math.max(3, longestRun(code, "`") + 1));
  return quotePrefix(`${fence}${str(node.language)}\n${code}\n${fence}`, Number(node.border ?? 0));
}

function list(node: Node, r: EntityResolver): string {
  const indent = "  ".repeat(Number(node.indent ?? 0));
  const offset = Number(node.offset ?? 0);
  const items = asNodes(node.elements).map((item, idx) => {
    const content = inline(item.elements, r);
    let prefix = "- ";
    if (node.style === "ordered") prefix = `${idx + 1 + offset}. `;
    else if (node.style === "check") prefix = `- [${item.checked ? "x" : " "}] `;
    const [first = "", ...rest] = content.split("\n");
    const pad = indent + " ".repeat(prefix.length);
    return [`${indent}${prefix}${first}`, ...rest.map((l) => `${pad}${l}`)].join("\n");
  });
  return quotePrefix(items.join("\n"), Number(node.border ?? 0));
}

function textObject(v: unknown, r: EntityResolver): string {
  if (!v || typeof v !== "object") return "";
  const t = v as { type?: string; text?: string };
  return t.type === "mrkdwn" ? mrkdwnToMarkdown(str(t.text), r) : str(t.text);
}

function table(node: Node, r: EntityResolver): string {
  const rows = Array.isArray(node.rows) ? (node.rows as unknown[][]) : [];
  if (rows.length === 0) return "";
  const cell = (c: unknown): string => {
    const n = (c ?? {}) as Node;
    const text = n.type === "rich_text" ? convertBlocksToMarkdown([n], r) : str(n.text);
    return text.replace(/\|/g, "\\|").replace(/\n/g, " ");
  };
  const lines = rows.map((row) => `| ${(Array.isArray(row) ? row : []).map(cell).join(" | ")} |`);
  const cols = Array.isArray(rows[0]) ? rows[0].length : 1;
  lines.splice(1, 0, `| ${Array.from({ length: cols }, () => "---").join(" | ")} |`);
  return lines.join("\n");
}

/** Converts a `blocks` array into standard Markdown. Returns "" when nothing renders. */
export function convertBlocksToMarkdown(blocks: unknown, r: EntityResolver = {}): string {
  const chunks: Array<{ text: string; kind: string }> = [];
  for (const block of asNodes(blocks)) {
    switch (block.type) {
      case "header": {
        const text = textObject(block.text, r);
        if (text) chunks.push({ text: `${"#".repeat(Number(block.level ?? 1) || 1)} ${text}`, kind: "header" });
        break;
      }
      case "divider":
        chunks.push({ text: "---", kind: "divider" });
        break;
      case "section": {
        const parts = [textObject(block.text, r), ...asNodes(block.fields).map((f) => textObject(f, r))].filter(Boolean);
        if (parts.length) chunks.push({ text: parts.join("\n"), kind: "section" });
        break;
      }
      case "context": {
        const parts = asNodes(block.elements)
          .map((e) => (e.type === "image" ? str(e.alt_text) : textObject(e, r)))
          .filter(Boolean);
        if (parts.length) chunks.push({ text: parts.join(" "), kind: "context" });
        break;
      }
      case "markdown": {
        const text = str(block.text);
        if (text) chunks.push({ text, kind: "markdown" });
        break;
      }
      case "table": {
        const text = table(block, r);
        if (text) chunks.push({ text, kind: "table" });
        break;
      }
      case "rich_text":
        for (const sub of asNodes(block.elements)) {
          let rendered = "";
          if (sub.type === "rich_text_section") rendered = inline(sub.elements, r);
          else if (sub.type === "rich_text_quote") rendered = quotePrefix(inline(sub.elements, r), Number(sub.border ?? 0) + 1);
          else if (sub.type === "rich_text_preformatted") rendered = preformatted(sub);
          else if (sub.type === "rich_text_list") rendered = list(sub, r);
          rendered = rendered.replace(/^\n+|\n+$/g, "");
          if (rendered) chunks.push({ text: rendered, kind: sub.type ?? "" });
        }
        break;
    }
  }
  return chunks
    .map((c, i) => {
      if (i === 0) return c.text;
      const prev = chunks[i - 1]!;
      const tight = prev.kind === "rich_text_list" && c.kind === "rich_text_list";
      return `${tight ? "\n" : "\n\n"}${c.text}`;
    })
    .join("");
}

const ENTITY = /<([@#!])([^>|]+)(?:\|([^>]*))?>|<((?:https?|mailto):[^>|]+)(?:\|([^>]*))?>/g;

/**
 * Converts Slack mrkdwn entities to Markdown: `<@U…>`, `<#C…|name>`, `<!here>`,
 * `<!subteam^S…|@handle>`, `<url|label>` and HTML escapes. Emphasis styling is kept
 * as-is because rewriting `*bold*` safely requires a full mrkdwn parser.
 */
export function mrkdwnToMarkdown(text: string, r: EntityResolver = {}): string {
  const replaced = text.replace(ENTITY, (_m, sigil?: string, id?: string, label?: string, url?: string, urlLabel?: string) => {
    if (url) return urlLabel && urlLabel !== url ? `[${urlLabel}](${url.replace(/\)/g, "%29")})` : url;
    if (sigil === "@" && id) {
      const name = r.userName?.(id) ?? label;
      return name ? `@${name.replace(/^@/, "")}` : `<@${id}>`;
    }
    if (sigil === "#" && id) {
      const name = r.channelName?.(id) ?? label;
      return name ? `#${name}` : `<#${id}>`;
    }
    if (sigil === "!" && id) {
      if (id.startsWith("subteam^")) {
        const sid = id.slice("subteam^".length);
        const handle = r.usergroupHandle?.(sid) ?? label?.replace(/^@/, "");
        return handle ? `@${handle}` : `<!subteam^${sid}>`;
      }
      if (id === "here" || id === "channel" || id === "everyone") return `@${id}`;
      return label ?? `<!${id}>`;
    }
    return _m;
  });
  return replaced.replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&amp;/g, "&");
}
