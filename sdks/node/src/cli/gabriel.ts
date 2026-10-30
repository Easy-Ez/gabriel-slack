#!/usr/bin/env node
import { createInterface } from "node:readline";
import { exchangeCookie } from "../core/auth.js";
import { DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS } from "../core/metadata.js";
import { GabrielError } from "../errors.js";
import { redactSecret } from "../util/redact.js";
import { browserLogin } from "./browser-login.js";
import { extractFromDebugChrome } from "./browser-extract.js";
import { deleteSession, loadSessionCookie, parseCurlForCookie, saveSession, sessionFile } from "./session-store.js";

const HELP = `gabriel — credential helper for gabriel-slack

Usage:
  gabriel auth login [domain]
      Open a dedicated Chrome window on the workspace sign-in page; after you sign in,
      the session cookie is verified and saved (0600). Nothing secret is printed.
  gabriel auth parse-curl [--save] [--workspace <domain>]
      Read a browser "Copy as cURL" request (any *.slack.com request) from stdin,
      extract the session cookie, verify it, and optionally save it (0600).
  gabriel auth extract [--port <n>] [--workspace <domain>]
      Read the session cookie from a Chrome you started with --remote-debugging-port
      (default 9222) and signed in to Slack, verify it, and save it (0600).
  gabriel auth status
      Verify the session from GABRIEL_COOKIE_D or the saved session and list workspaces.
  gabriel auth logout --yes
      Delete the saved session file.

Credentials are never printed; only prefixes and lengths are shown.
`;

async function readStdin(): Promise<string> {
  if (process.stdin.isTTY) {
    process.stderr.write("Paste the cURL command, then press Ctrl-D:\n");
  }
  const chunks: string[] = [];
  const rl = createInterface({ input: process.stdin });
  for await (const line of rl) chunks.push(line);
  return chunks.join("\n");
}

async function describeSession(cookieD: string): Promise<void> {
  const { teams } = await exchangeCookie({ fetch, metadata: { ...DEFAULT_CLIENT_METADATA }, endpoints: { ...DEFAULT_ENDPOINTS } }, cookieD);
  process.stdout.write(`Session cookie ${redactSecret(cookieD)} is signed in to ${teams.length} workspace(s):\n`);
  for (const t of teams) {
    process.stdout.write(`  - ${t.domain} (${t.id}) user=${t.userId} token=${redactSecret(t.token)}\n`);
  }
}

function flag(args: string[], name: string): string | undefined {
  const i = args.indexOf(name);
  return i >= 0 ? args[i + 1] : undefined;
}

export async function main(argv: string[]): Promise<number> {
  const [group, cmd, ...rest] = argv;
  if (group !== "auth" || !cmd || cmd === "help" || argv.includes("--help")) {
    process.stdout.write(HELP);
    return group === undefined || argv.includes("--help") || cmd === "help" ? 0 : 1;
  }
  switch (cmd) {
    case "parse-curl": {
      const cookieD = parseCurlForCookie(await readStdin());
      if (!cookieD) {
        process.stderr.write("No `d=xoxd-…` cookie found in the input.\n");
        return 1;
      }
      await describeSession(cookieD);
      if (rest.includes("--save")) {
        const workspace = flag(rest, "--workspace");
        const file = saveSession({ cookieD, ...(workspace ? { workspace } : {}) });
        process.stdout.write(`Saved to ${file} (mode 0600).\n`);
      }
      return 0;
    }
    case "status": {
      const session = loadSessionCookie();
      if (!session) {
        process.stderr.write(`No session: set GABRIEL_COOKIE_D or run \`gabriel auth parse-curl --save\` (${sessionFile()}).\n`);
        return 1;
      }
      await describeSession(session.cookieD);
      return 0;
    }
    case "logout": {
      if (!rest.includes("--yes")) {
        process.stderr.write(`This deletes ${sessionFile()}. Re-run with --yes to confirm.\n`);
        return 1;
      }
      process.stdout.write(deleteSession() ? "Saved session deleted.\n" : "No saved session.\n");
      return 0;
    }
    case "login": {
      const domain = rest.find((a) => !a.startsWith("--"));
      const cookieD = await browserLogin(domain);
      await describeSession(cookieD);
      const file = saveSession({ cookieD, ...(domain ? { workspace: domain } : {}) });
      process.stdout.write(`Saved to ${file} (mode 0600).\n`);
      return 0;
    }
    case "extract": {
      const portArg = flag(rest, "--port");
      const cookieD = await extractFromDebugChrome(portArg ? Number(portArg) : 9222);
      await describeSession(cookieD);
      const domain = flag(rest, "--workspace");
      const file = saveSession({ cookieD, ...(domain ? { workspace: domain } : {}) });
      process.stdout.write(`Saved to ${file} (mode 0600).\n`);
      return 0;
    }
    default:
      process.stdout.write(HELP);
      return 1;
  }
}

const isEntry = process.argv[1] !== undefined && /gabriel(\.js|\.ts)?$/.test(process.argv[1]);
if (isEntry) {
  main(process.argv.slice(2)).then(
    (code) => process.exit(code),
    (err: unknown) => {
      const msg = err instanceof GabrielError ? `${err.code}: ${err.message}` : String(err);
      process.stderr.write(`error: ${msg}\n`);
      process.exit(1);
    },
  );
}
