import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import WebSocket from "ws";
import { exchangeCookie, findTeam } from "../core/auth.js";
import { DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS } from "../core/metadata.js";
import { GabrielError } from "../errors.js";

const CHROME_CANDIDATES = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
];

function findChrome(): string {
  const explicit = process.env.GABRIEL_CHROME_PATH;
  if (explicit) {
    if (!existsSync(explicit)) throw new GabrielError("INVALID_ARGUMENT", "GABRIEL_CHROME_PATH does not exist");
    return explicit;
  }
  const found = CHROME_CANDIDATES.find((p) => existsSync(p));
  if (!found) throw new GabrielError("INVALID_STATE", "Chrome/Chromium not found; set GABRIEL_CHROME_PATH");
  return found;
}

function cdpCall(ws: WebSocket, id: number, method: string): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const onMessage = (raw: WebSocket.RawData) => {
      let msg: { id?: number; error?: { message: string }; result?: Record<string, unknown> };
      try {
        msg = JSON.parse(raw.toString());
      } catch {
        return;
      }
      if (msg.id !== id) return;
      ws.off("message", onMessage);
      if (msg.error) reject(new Error(msg.error.message));
      else resolve(msg.result ?? {});
    };
    ws.on("message", onMessage);
    ws.send(JSON.stringify({ id, method }));
  });
}

/**
 * Opens a dedicated Chrome window (fresh temporary profile) on the workspace sign-in
 * page. The person signs in themselves; once the `.slack.com` `d` cookie can be
 * exchanged for a workspace token, the window closes and the cookie is returned.
 * The cookie is never printed. The temporary profile is deleted afterwards.
 */
export async function browserLogin(domain: string | undefined, opts: { timeoutMs?: number; log?: (line: string) => void } = {}): Promise<string> {
  const log = opts.log ?? ((l: string) => process.stderr.write(`${l}\n`));
  const deps = { fetch, metadata: { ...DEFAULT_CLIENT_METADATA }, endpoints: { ...DEFAULT_ENDPOINTS } };
  let loginUrl = "https://slack.com/signin";
  if (domain) {
    const team = await findTeam(deps, domain).catch(() => undefined);
    if (team?.ok === true && typeof team.url === "string" && /^https:\/\/[a-z0-9-]+\.slack\.com\/?$/i.test(team.url)) loginUrl = team.url;
  }
  const chrome = findChrome();
  const profile = mkdtempSync(path.join(os.tmpdir(), "gabriel-login-"));
  const port = 19222 + Math.floor(Math.random() * 1000);
  log(`Opening a sign-in window at ${loginUrl} — complete the Slack sign-in there; it closes automatically.`);
  const proc = spawn(chrome, [`--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "--no-first-run", "--no-default-browser-check", `--app=${loginUrl}`], {
    stdio: "ignore",
  });
  let ws: WebSocket | undefined;
  try {
    let browserWsUrl = "";
    for (let i = 0; i < 50 && !browserWsUrl; i++) {
      try {
        const info = (await (await fetch(`http://127.0.0.1:${port}/json/version`)).json()) as { webSocketDebuggerUrl?: string };
        browserWsUrl = info.webSocketDebuggerUrl ?? "";
      } catch {
        await new Promise((r) => setTimeout(r, 300));
      }
    }
    if (!browserWsUrl) throw new GabrielError("INVALID_STATE", "could not reach the sign-in browser window");
    const socket = new WebSocket(browserWsUrl);
    ws = socket;
    await new Promise<void>((resolve, reject) => {
      socket.once("open", () => resolve());
      socket.once("error", reject);
    });
    const deadline = Date.now() + (opts.timeoutMs ?? 300_000);
    let seq = 1;
    while (Date.now() < deadline) {
      if (proc.exitCode !== null) throw new GabrielError("CANCELLED", "the sign-in window was closed before signing in");
      const { cookies } = (await cdpCall(socket, seq++, "Storage.getCookies")) as { cookies?: Array<{ name: string; domain: string; value: string }> };
      const d = cookies?.find((c) => c.name === "d" && /(^|\.)slack\.com$/.test(c.domain.replace(/^\./, "")) && decodeURIComponent(c.value).startsWith("xoxd-"));
      if (d) {
        try {
          await exchangeCookie(deps, d.value);
          return d.value;
        } catch {
          /* still redirecting through SSO; keep waiting */
        }
      }
      await new Promise((r) => setTimeout(r, 1000));
    }
    throw new GabrielError("NETWORK_TIMEOUT", "timed out waiting for sign-in");
  } finally {
    ws?.close();
    proc.kill("SIGTERM");
    await new Promise((r) => setTimeout(r, 500));
    rmSync(profile, { recursive: true, force: true });
  }
}
