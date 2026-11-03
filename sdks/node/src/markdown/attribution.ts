import { GabrielError } from "../errors.js";
import { convertBlocksToMarkdown, type EntityResolver } from "./blocks-to-markdown.js";
import { EMOJI_WEIGHT, measureMarkdown } from "./chunker.js";

/**
 * Agent attribution line (spec/behavior.md §9.1): a context block above the message body
 * that says an agent wrote it. It is a self-declared display hint — any client can write
 * the same block — never proof of who wrote a message.
 */

/**
 * How the line ends (`@Me` is the sending account):
 * - `confirmed`   → `… · confirmed by @Me`
 * - `unconfirmed` → `… · sent for @Me · not reviewed`
 * - `hidden`      → nothing
 */
export type AttributionReview = "confirmed" | "unconfirmed" | "hidden";

/**
 * Preset line. `{ icon: ":robot_face:", name: "Paimon", role: "Assistant", review: "confirmed" }`
 * renders `:robot_face: Paimon (Assistant) · confirmed by <@SELF>`.
 */
export interface AttributionPreset {
  /** Leading emoji shortcode. Default `":robot_face:"`; `null` omits it. */
  icon?: string | null;
  /** Agent name. Required, single line, at most 64 characters. */
  name: string;
  /** Shown in parentheses after the name. Optional, at most 64 characters. */
  role?: string;
  /** Default `"unconfirmed"`. */
  review?: AttributionReview;
}

export interface AttributionInfo {
  selfUserId: string;
  channel: string;
  threadTs?: string;
  review: AttributionReview;
}

/**
 * Custom line written as given (mrkdwn, at most 3,000 effective characters). `{self}` becomes the
 * sending account's mention: `{ text: ":sparkles: drafted by *Paimon*, reviewed by {self}" }`.
 */
export interface AttributionCustom {
  text: string | ((info: AttributionInfo) => string);
  /** Passed to a `text` function and recorded in the block ID. Default `"unconfirmed"`. */
  review?: AttributionReview;
}

export type Attribution = AttributionPreset | AttributionCustom;

/** Client default. `review: "confirmed"` is accepted only per write. */
export type DefaultAttribution =
  | (Omit<AttributionPreset, "review"> & { review?: "unconfirmed" | "hidden" })
  | (Omit<AttributionCustom, "review"> & { review?: "unconfirmed" | "hidden" });

/** Per-write override: `false` disables it, a partial preset is merged over the client default. */
export type AttributionOverride = false | Partial<AttributionPreset> | AttributionCustom;

/** The exact line and block a write would carry. */
export interface RenderedAttribution {
  /** mrkdwn of the line. */
  text: string;
  block: { type: "context"; block_id: string; elements: [{ type: "mrkdwn"; text: string }] };
}

/** Attribution found on a received message. */
export interface MessageAttribution {
  /** The line rendered to Markdown. */
  text: string;
  /** Present when the line was written by a preset. */
  review?: AttributionReview;
}

export const ATTRIBUTION_BLOCK_ID = "gabriel.attribution";
export const DEFAULT_ATTRIBUTION_ICON = ":robot_face:";
const MAX_FIELD = 64;
const MAX_TEXT = 3_000;
const REVIEWS: ReadonlySet<string> = new Set(["confirmed", "unconfirmed", "hidden"]);
const ICON = /^:[a-z0-9_+'-]+:(?::skin-tone-[2-6]:)?$/i;
const BROADCAST = /<!(?:channel|here|everyone|subteam\^)/i;

function invalid(message: string): GabrielError {
  return new GabrielError("INVALID_ARGUMENT", `attribution: ${message}`);
}

function isCustom(a: Attribution | Partial<AttributionPreset> | AttributionCustom): a is AttributionCustom {
  return "text" in a && a.text !== undefined;
}

function escapeMrkdwn(text: string): string {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function field(value: unknown, label: string, required: boolean): string | undefined {
  if (value === undefined && !required) return undefined;
  if (typeof value !== "string") throw invalid(`\`${label}\` must be a string`);
  const v = value.trim();
  if (!v) {
    if (required) throw invalid(`\`${label}\` must not be empty`);
    return undefined;
  }
  if (/[\r\n]/.test(v)) throw invalid(`\`${label}\` must be a single line`);
  if (Array.from(v).length > MAX_FIELD) throw invalid(`\`${label}\` must be at most ${MAX_FIELD} characters`);
  return v;
}

function checkReview(review: unknown, allowConfirmed: boolean): void {
  if (review === undefined) return;
  if (typeof review !== "string" || !REVIEWS.has(review)) throw invalid('`review` must be "confirmed", "unconfirmed" or "hidden"');
  if (review === "confirmed" && !allowConfirmed) {
    throw invalid('`review: "confirmed"` is per write only — pass it when a person actually reviewed that message');
  }
}

function checkText(text: string): string {
  if (!text.trim()) throw invalid("`text` must not be empty");
  if (measureMarkdown(text).effective > MAX_TEXT) throw invalid(`\`text\` must be at most ${MAX_TEXT} characters (emoji count as ${EMOJI_WEIGHT})`);
  if (BROADCAST.test(text)) throw invalid("`text` must not contain @channel, @here, @everyone or user group mentions");
  return text;
}

/** Validates an attribution; `scope: "client"` additionally rejects `review: "confirmed"`. */
export function validateAttribution(a: Attribution, scope: "client" | "write" = "write"): void {
  if (!a || typeof a !== "object") throw invalid("must be an object");
  checkReview(a.review, scope === "write");
  if (isCustom(a)) {
    if (typeof a.text === "string") checkText(a.text);
    else if (typeof a.text !== "function") throw invalid("`text` must be a string or a function");
    return;
  }
  field(a.name, "name", true);
  field(a.role, "role", false);
  if (a.icon !== undefined && a.icon !== null && (typeof a.icon !== "string" || !ICON.test(a.icon))) {
    throw invalid('`icon` must be an emoji shortcode such as ":robot_face:", or null');
  }
}

/** Applies a per-write override to the client default. Returns null when attribution is off. */
export function resolveAttribution(base: DefaultAttribution | undefined, override: AttributionOverride | undefined): Attribution | null {
  if (override === false) return null;
  if (override === undefined) return base ?? null;
  if (isCustom(override)) return override;
  if (base && isCustom(base) && override.name === undefined) {
    return override.review === undefined ? base : { ...base, review: override.review };
  }
  const merged = { ...(base && !isCustom(base) ? base : {}), ...override } as Partial<AttributionPreset>;
  if (merged.name === undefined) throw invalid("`name` is required (set it in the client's `attribution` option or on this write)");
  return merged as AttributionPreset;
}

/**
 * Renders the line and block a write would carry.
 *
 * `renderAttribution({ name: "Paimon", role: "Assistant", review: "confirmed" }, { selfUserId: "U0ALICE0001", channel: "C0GENERAL01" })`
 * → `text: ":robot_face: Paimon (Assistant) · confirmed by <@U0ALICE0001>"`.
 */
export function renderAttribution(a: Attribution, where: { selfUserId: string; channel: string; threadTs?: string }): RenderedAttribution {
  validateAttribution(a);
  const review = a.review ?? "unconfirmed";
  const self = `<@${where.selfUserId}>`;
  let text: string;
  if (isCustom(a)) {
    const raw =
      typeof a.text === "function"
        ? a.text({ selfUserId: where.selfUserId, channel: where.channel, ...(where.threadTs ? { threadTs: where.threadTs } : {}), review })
        : a.text;
    if (typeof raw !== "string") throw invalid("`text` function must return a string");
    text = checkText(raw.split("{self}").join(self));
  } else {
    const icon = a.icon === undefined ? DEFAULT_ATTRIBUTION_ICON : a.icon;
    const role = field(a.role, "role", false);
    const head = `${escapeMrkdwn(field(a.name, "name", true)!)}${role ? ` (${escapeMrkdwn(role)})` : ""}`;
    const suffix = review === "confirmed" ? ` · confirmed by ${self}` : review === "unconfirmed" ? ` · sent for ${self} · not reviewed` : "";
    text = `${icon ? `${icon} ` : ""}${head}${suffix}`;
  }
  const blockId = `${ATTRIBUTION_BLOCK_ID}.${isCustom(a) ? "custom" : review}`;
  return { text, block: { type: "context", block_id: blockId, elements: [{ type: "mrkdwn", text }] } };
}

function isAttributionBlock(block: unknown): block is Record<string, unknown> {
  if (!block || typeof block !== "object") return false;
  const b = block as Record<string, unknown>;
  if (b.type !== "context" || typeof b.block_id !== "string") return false;
  return b.block_id === ATTRIBUTION_BLOCK_ID || b.block_id.startsWith(`${ATTRIBUTION_BLOCK_ID}.`);
}

/** Splits a leading attribution block off a message's `blocks`. */
export function splitAttribution(blocks: unknown): { block?: Record<string, unknown>; rest: unknown } {
  if (!Array.isArray(blocks) || !isAttributionBlock(blocks[0])) return { rest: blocks };
  return { block: blocks[0], rest: blocks.slice(1) };
}

/** Parses a received message's attribution, if its first block is one. */
export function parseAttribution(blocks: unknown, r: EntityResolver = {}): MessageAttribution | undefined {
  const { block } = splitAttribution(blocks);
  if (!block) return undefined;
  const suffix = String(block.block_id).slice(ATTRIBUTION_BLOCK_ID.length + 1);
  return { text: convertBlocksToMarkdown([block], r), ...(REVIEWS.has(suffix) ? { review: suffix as AttributionReview } : {}) };
}

/** Plain-text fallback (`text` field) for a Markdown body sent as blocks: notifications and search snippets. */
export function markdownToPlainText(markdown: string, max = MAX_TEXT): string {
  const plain = markdown
    .replace(/^ {0,3}(`{3,}|~{3,})[^\n]*\n?/gm, "")
    .replace(/!?\[([^\]]*)\]\(([^)\s]+)[^)]*\)/g, "$1")
    .replace(/^ {0,3}#{1,6}\s+/gm, "")
    .replace(/^ {0,3}>\s?/gm, "")
    .replace(/(\*\*|__|~~)(?=\S)([\s\S]*?\S)\1/g, "$2")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
  const chars = Array.from(plain);
  return chars.length <= max ? plain : `${chars.slice(0, max - 1).join("")}…`;
}
