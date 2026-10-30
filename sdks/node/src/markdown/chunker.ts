/**
 * Syntax-aware Markdown chunker (spec/behavior.md §9).
 *
 * Slack validates `markdown_text` twice: raw Unicode code points (<= 12,000,
 * `msg_too_long`) and the length after emoji are expanded to `:shortcode:`
 * (`msg_blocks_too_long`). Without a full shortcode table every pictographic
 * emoji is conservatively weighted as 20 characters.
 */
export const SERVER_MAX_CODE_POINTS = 12_000;
export const DEFAULT_CHUNK_BUDGET = 11_500;
export const DEFAULT_CHUNK_MIN = 8_000;
export const EMOJI_WEIGHT = 20;

const PICTOGRAPHIC = /\p{Extended_Pictographic}/u;

export interface LengthMeasure {
  codePoints: number;
  emojiCount: number;
  /** `max(rawCodePoints, expandedBlocksLength)` — the value compared against the budget. */
  effective: number;
}

/**
 * Multipart form encoding normalizes every line break to CRLF before the server
 * counts code points, so a newline is weighted as 2.
 */
export function measureMarkdown(text: string): LengthMeasure {
  let codePoints = 0;
  let emojiCount = 0;
  let newlines = 0;
  for (const cp of text) {
    codePoints++;
    if (cp === "\n") newlines++;
    else if (PICTOGRAPHIC.test(cp)) emojiCount++;
  }
  return { codePoints, emojiCount, effective: codePoints + newlines + emojiCount * (EMOJI_WEIGHT - 1) };
}

interface FenceState {
  marker: string;
  lang: string;
}

const FENCE_LINE = /^\s{0,3}(`{3,}|~{3,})(.*)$/;

/** Returns the code fence still open at the end of `text`, if any. */
export function openFenceAt(text: string): FenceState | undefined {
  let open: FenceState | undefined;
  for (const line of text.split("\n")) {
    const m = FENCE_LINE.exec(line);
    if (!m) continue;
    const marker = m[1]!;
    const rest = m[2] ?? "";
    if (!open) {
      if (marker.startsWith("`") && rest.includes("`")) continue; // inline code, not a fence
      open = { marker, lang: rest.trim() };
    } else if (marker[0] === open.marker[0] && marker.length >= open.marker.length && rest.trim() === "") {
      open = undefined;
    }
  }
  return open;
}

/** Appends a closing fence when `text` ends inside a code block (render-time copy only). */
export function healOpenFence(text: string): string {
  const open = openFenceAt(text);
  if (!open) return text;
  return `${text}${text.endsWith("\n") ? "" : "\n"}${open.marker}`;
}

/** Cumulative effective weight → string index (code-point safe). */
function indexAtWeight(text: string, weight: number): number {
  let acc = 0;
  let idx = 0;
  for (const cp of text) {
    const w = cp === "\n" ? 2 : PICTOGRAPHIC.test(cp) ? EMOJI_WEIGHT : 1;
    if (acc + w > weight) return idx;
    acc += w;
    idx += cp.length;
  }
  return text.length;
}

/** `[start, end)` ranges covered by fenced code blocks (an unclosed fence runs to the end). */
function fenceRanges(text: string): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];
  let open: { marker: string; start: number } | undefined;
  let pos = 0;
  for (const line of text.split("\n")) {
    const m = FENCE_LINE.exec(line);
    if (m) {
      const marker = m[1]!;
      const rest = m[2] ?? "";
      if (!open) {
        if (!(marker.startsWith("`") && rest.includes("`"))) open = { marker, start: pos };
      } else if (marker[0] === open.marker[0] && marker.length >= open.marker.length && rest.trim() === "") {
        ranges.push([open.start, pos + line.length]);
        open = undefined;
      }
    }
    pos += line.length + 1;
  }
  if (open) ranges.push([open.start, text.length]);
  return ranges;
}

function lastIndexOutsideFence(text: string, ranges: Array<[number, number]>, needle: string, from: number, to: number): number {
  let idx = text.lastIndexOf(needle, to);
  while (idx >= from) {
    const inside = ranges.some(([s, e]) => idx > s && idx < e);
    if (!inside) return idx;
    idx = text.lastIndexOf(needle, idx - 1);
  }
  return -1;
}

function lineStart(text: string, idx: number): number {
  return text.lastIndexOf("\n", idx - 1) + 1;
}

/** If `idx` (a line start) sits inside a pipe table, returns the table's header + separator rows. */
function tableHeaderBefore(text: string, idx: number): string | undefined {
  const nextLineEnd = text.indexOf("\n", idx);
  const nextLine = text.slice(idx, nextLineEnd === -1 ? undefined : nextLineEnd);
  if (!nextLine.trimStart().startsWith("|")) return undefined;
  const lines = text.slice(0, idx).replace(/\n$/, "").split("\n");
  let start = lines.length - 1;
  while (start >= 0 && lines[start]!.trimStart().startsWith("|")) start--;
  start++;
  const header = lines[start];
  const sep = lines[start + 1];
  if (header === undefined || sep === undefined || !/^\s*\|?\s*:?-{3,}/.test(sep)) return undefined;
  if (start + 1 >= lines.length - 1 && lines.length - start <= 2) return undefined; // cut right after the header
  return `${header}\n${sep}`;
}

interface Cut {
  index: number;
  skip: number;
  midLine: boolean;
}

function findCut(text: string, budget: number, min: number, reserve: number): Cut {
  const maxIdx = Math.max(1, indexAtWeight(text, budget - reserve));
  const minIdx = Math.min(maxIdx, indexAtWeight(text, Math.min(min, budget - reserve)));
  const ranges = fenceRanges(text.slice(0, maxIdx + 2));
  let i = lastIndexOutsideFence(text, ranges, "\n\n", minIdx, maxIdx);
  if (i > 0) return { index: i, skip: 2, midLine: false };
  i = lastIndexOutsideFence(text, ranges, "\n", minIdx, maxIdx);
  if (i > 0) return { index: i, skip: 1, midLine: false };
  i = text.lastIndexOf("\n", maxIdx);
  if (i >= minIdx && i > 0) return { index: i, skip: 1, midLine: false };
  const window = text.slice(minIdx, maxIdx);
  const ws = window.search(/\s\S*$/);
  if (ws >= 0) return { index: minIdx + ws, skip: 1, midLine: true };
  return { index: maxIdx, skip: 0, midLine: true };
}

/**
 * Splits Markdown into pieces whose effective length fits `budget`, preferring blank
 * lines → newlines → word boundaries outside code blocks. Code fences cut mid-block
 * are closed and reopened (same language); tables repeat their header; a line split
 * mid-way keeps its quote / indent prefix.
 */
export function chunkMarkdown(text: string, budget = DEFAULT_CHUNK_BUDGET, min = DEFAULT_CHUNK_MIN): string[] {
  if (budget < 200) throw new RangeError("chunk budget too small");
  const chunks: string[] = [];
  let rest = text;
  let guard = 0;
  while (measureMarkdown(rest).effective > budget) {
    if (++guard > 10_000) throw new Error("chunker failed to make progress");
    const cut = findCut(rest, budget, min, 64);
    let piece = rest.slice(0, cut.index);
    let next = rest.slice(cut.index + cut.skip);
    const fence = openFenceAt(piece);
    if (fence) {
      piece = `${piece.replace(/\n$/, "")}\n${fence.marker}`;
      next = `${fence.marker}${fence.lang}\n${next}`;
    } else if (cut.midLine) {
      const line = rest.slice(lineStart(rest, cut.index), cut.index);
      const prefix = /^(\s*(?:>\s?)*)/.exec(line)?.[1] ?? "";
      next = `${prefix}${next.replace(/^\s+/, "")}`;
    } else {
      const header = tableHeaderBefore(rest, cut.index + cut.skip);
      if (header) next = `${header}\n${next}`;
    }
    piece = piece.replace(/\s+$/, "");
    if (piece.length > 0) chunks.push(piece);
    rest = next;
  }
  if (rest.trim().length > 0 || chunks.length === 0) chunks.push(rest);
  return chunks;
}
