#!/usr/bin/env node
// Scans every file git would track (tracked + untracked, minus .gitignore) for credentials,
// real-looking Slack IDs, personal paths, internal identifiers, research / review markers and
// docs outside the curated public set. Run before every commit.
import { execFileSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const files = execFileSync("git", ["ls-files", "-co", "--exclude-standard"], { cwd: root, encoding: "utf8" })
  .split("\n")
  .filter((f) => f && !/(^|\/)(pnpm-lock\.yaml|package-lock\.json)$/.test(f) && !/\.(png|jpe?g|gif|ico|woff2?)$/i.test(f));

// Placeholder IDs: type letter + "0" + (9–10 digits | uppercase word + digits), e.g. U0123456789, C0GENERAL01.
const PLACEHOLDER = /^[TCDGUWBSFEA]0(?:\d{9,10}|[A-Z]{3,}\d*)$/;
const ID = /\b[TCDGUWBSFEA][A-Z0-9]{8,11}\b/g;
const TOKEN = /xox[abcdeposr]-[A-Za-z0-9-]{12,}/g;
const ALLOWED_TOKENS = /^xox[a-z]-(test|your|xxx|placeholder|rotated|12345)/i;
const EMAIL = /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g;
const ALLOWED_EMAIL = /@(example\.(com|org)|anthropic\.com)$/;
const LOCAL_PATH = /\/(Users|home)\/[A-Za-z0-9._-]+\//g;
const ID_STOPWORDS = new Set(["CANCELLED", "SUBSCRIBE", "UNSUBSCRIBE", "DISCONNECTED", "CONNECTING", "BACKOFF", "TIMESTAMP"]);

// Curated public docs: anything else under docs/ is rejected (research notes stay in the private repo).
const PUBLIC_DOCS = new Set(["docs/README.md", "docs/architecture.md", "docs/zh-CN/guide.md"]);

// Process / research markers that belong to internal notes, not to public files.
const INTERNAL_MARKERS = [
  [/MCP\s*(实测|Verified)|已实测验证|实测验证/, "verification marker"],
  [/\b[VS]-\d{1,2}\b/, "verification case id"],
  [/模块\s*`|__wpRequire|rspackChunk/, "front-end module analysis reference"],
  [/🆕|附录\s*[AB]|评审决议|准入/, "draft / review marker"],
  [/\bdocs\/0\d/, "reference to internal research docs"],
];

// Optional local-only list of internal words (git-ignored), one case-insensitive substring per line.
const denyFile = path.join(root, ".oss-denylist.txt");
const deny = existsSync(denyFile)
  ? readFileSync(denyFile, "utf8").split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#"))
  : [];

const findings = [];
for (const rel of files) {
  if (rel === "scripts/oss-leak-check.mjs") continue;
  if (rel.startsWith("docs/") && !PUBLIC_DOCS.has(rel)) findings.push(`${rel} not in PUBLIC_DOCS (internal notes belong in the private repo)`);
  let text;
  try {
    text = readFileSync(path.join(root, rel), "utf8");
  } catch {
    continue;
  }
  text.split("\n").forEach((line, i) => {
    const at = `${rel}:${i + 1}`;
    for (const m of line.matchAll(TOKEN)) if (!ALLOWED_TOKENS.test(m[0])) findings.push(`${at} token-like value ${m[0].slice(0, 8)}…`);
    for (const m of line.matchAll(ID)) {
      const id = m[0];
      if (PLACEHOLDER.test(id) || ID_STOPWORDS.has(id) || !/\d/.test(id) || /^[A-Z]+_/.test(id)) continue;
      findings.push(`${at} non-placeholder ID ${id}`);
    }
    for (const m of line.matchAll(EMAIL)) if (!ALLOWED_EMAIL.test(m[0])) findings.push(`${at} email ${m[0]}`);
    for (const m of line.matchAll(LOCAL_PATH)) findings.push(`${at} local path ${m[0]}`);
    for (const [re, what] of INTERNAL_MARKERS) if (re.test(line)) findings.push(`${at} ${what}`);
    for (const word of deny) if (line.toLowerCase().includes(word.toLowerCase())) findings.push(`${at} internal identifier "${word}"`);
  });
}

if (findings.length) {
  console.error(`✗ ${findings.length} potential leak(s):\n${findings.map((f) => `  ${f}`).join("\n")}`);
  process.exit(1);
}
console.log(`✓ ${files.length} files scanned, no leaks found`);
