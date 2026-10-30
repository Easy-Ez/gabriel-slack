/**
 * Redaction helpers. Credentials must never appear in logs, errors or events:
 * only a short prefix and the length are kept, e.g. `xoxc-30235...[REDACTED, len=109]`.
 */
export function redactSecret(value: string | undefined, visiblePrefix = 10): string {
  if (!value) return "<empty>";
  return `${value.slice(0, Math.min(visiblePrefix, Math.floor(value.length / 4)))}...[REDACTED, len=${value.length}]`;
}

const SECRET_PATTERNS: Array<[RegExp, string]> = [
  [/xox[a-z]-[A-Za-z0-9%\-_.]+/g, "xox?-[REDACTED]"],
  [/(\b(?:token|frt)=)[^&\s"']+/g, "$1[REDACTED]"],
  [/(\bd=)[^;\s"']+/g, "$1[REDACTED]"],
  [/(Bearer\s+)[^\s"']+/gi, "$1[REDACTED]"],
];

/** Scrubs anything that looks like a Slack token, cookie, or frt ticket from free text. */
export function scrubSecrets(text: string): string {
  let out = text;
  for (const [pattern, replacement] of SECRET_PATTERNS) out = out.replace(pattern, replacement);
  return out;
}
