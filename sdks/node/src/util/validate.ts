import { GabrielError } from "../errors.js";

const CHANNEL_ID = /^[CDG][A-Z0-9]{2,30}$/;
const USER_ID = /^[UWB][A-Z0-9]{2,30}$/;
const TEAM_ID = /^[TE][A-Z0-9]{2,30}$/;
const FILE_ID = /^F[A-Z0-9]{2,30}$/;
const SUBTEAM_ID = /^S[A-Z0-9]{2,30}$/;
const TS = /^\d{1,12}\.\d{1,8}$/;
const EMOJI = /^[a-z0-9_+\-'’]{1,100}(::skin-tone-[2-6])?$/i;

function fail(field: string, value: unknown, expected: string): never {
  const shown = typeof value === "string" ? JSON.stringify(value.slice(0, 40)) : typeof value;
  throw new GabrielError("INVALID_ARGUMENT", `${field}: expected ${expected}, got ${shown}`);
}

export function assertChannelId(value: unknown, field = "channel"): string {
  if (typeof value !== "string" || !CHANNEL_ID.test(value)) fail(field, value, "a channel ID (C…/G…/D…)");
  return value;
}

export function assertUserId(value: unknown, field = "userId"): string {
  if (typeof value !== "string" || !USER_ID.test(value)) fail(field, value, "a user ID (U…/W…)");
  return value;
}

export function assertTeamId(value: unknown, field = "teamId"): string {
  if (typeof value !== "string" || !TEAM_ID.test(value)) fail(field, value, "a team ID (T…/E…)");
  return value;
}

export function assertFileId(value: unknown, field = "fileId"): string {
  if (typeof value !== "string" || !FILE_ID.test(value)) fail(field, value, "a file ID (F…)");
  return value;
}

export function isSubteamId(value: unknown): value is string {
  return typeof value === "string" && SUBTEAM_ID.test(value);
}

export function assertTs(value: unknown, field = "ts"): string {
  if (typeof value !== "string" || !TS.test(value)) fail(field, value, "a Slack timestamp like 1700000000.000100");
  return value;
}

export function isTs(value: unknown): value is string {
  return typeof value === "string" && TS.test(value);
}

/** Normalizes `":eyes:"` → `"eyes"` and validates the emoji name. */
export function normalizeEmoji(value: unknown, field = "emoji"): string {
  if (typeof value !== "string") fail(field, value, "an emoji name");
  const name = value.trim().replace(/^:/, "").replace(/:$/, "");
  if (!EMOJI.test(name)) fail(field, value, "an emoji name like \"eyes\"");
  return name;
}

export function assertNonEmptyString(value: unknown, field: string, maxLength = 1_000_000): string {
  if (typeof value !== "string" || value.length === 0 || value.length > maxLength) {
    fail(field, value, `a non-empty string (<= ${maxLength} chars)`);
  }
  return value;
}

export function assertPositiveInt(value: unknown, field: string, max = Number.MAX_SAFE_INTEGER): number {
  if (typeof value !== "number" || !Number.isInteger(value) || value <= 0 || value > max) {
    fail(field, value, `a positive integer <= ${max}`);
  }
  return value;
}

/**
 * Compares two Slack timestamps ("seconds.micros") numerically without float rounding.
 * Returns <0, 0, >0 like a comparator.
 */
export function compareTs(a: string, b: string): number {
  const [as = "0", af = ""] = a.split(".");
  const [bs = "0", bf = ""] = b.split(".");
  const secDiff = Number(as) - Number(bs);
  if (secDiff !== 0) return secDiff;
  const width = Math.max(af.length, bf.length);
  return Number(af.padEnd(width, "0")) - Number(bf.padEnd(width, "0"));
}

export function maxTs(a: string | undefined, b: string | undefined): string | undefined {
  if (a === undefined) return b;
  if (b === undefined) return a;
  return compareTs(a, b) >= 0 ? a : b;
}
