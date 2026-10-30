import WebSocket from "ws";
import { exchangeCookie } from "../core/auth.js";
import { DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS } from "../core/metadata.js";
import { GabrielError } from "../errors.js";

/**
 * Reads the `.slack.com` `d` cookie from a Chrome you started with a remote
 * debugging port, via the DevTools (CDP) `Storage.getCookies` endpoint. This is a
 * deliberate, user-opened door — Chrome does not expose this port by default.
 *
 * It does NOT read Chrome's encrypted cookie files on disk: that is fragile,
 * OS-specific and the wrong trust boundary. Start Chrome like this first, sign in,
 * then run `gabriel auth extract`:
 *
 *   Google Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/slack-profile
 */
function cdpCall(ws: WebSocket, id: number, method: string): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      ws.off("message", onMessage);
      reject(new Error(`CDP ${method} timed out`));
    }, 10_000);
    const onMessage = (raw: WebSocket.RawData) => {
      let msg: { id?: number; error?: { message: string }; result?: Record<string, unknown> };
      try {
        msg = JSON.parse(raw.toString());
      } catch {
        return;
      }
      if (msg.id !== id) return;
      clearTimeout(timer);
      ws.off("message", onMessage);
      if (msg.error) reject(new Error(msg.error.message));
      else resolve(msg.result ?? {});
    };
    ws.on("message", onMessage);
    ws.send(JSON.stringify({ id, method }));
  });
}

export async function extractFromDebugChrome(port: number): Promise<string> {
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new GabrielError("INVALID_ARGUMENT", "debug port must be 1–65535");
  let wsUrl: string;
  try {
    const info = (await (await fetch(`http://127.0.0.1:${port}/json/version`)).json()) as { webSocketDebuggerUrl?: string };
    if (!info.webSocketDebuggerUrl) throw new Error("no webSocketDebuggerUrl");
    wsUrl = info.webSocketDebuggerUrl;
  } catch {
    throw new GabrielError(
      "INVALID_STATE",
      `no Chrome DevTools endpoint on 127.0.0.1:${port}. Start Chrome with --remote-debugging-port=${port} --user-data-dir=<dir>, sign in to Slack, then retry.`,
    );
  }
  const ws = new WebSocket(wsUrl);
  try {
    await new Promise<void>((resolve, reject) => {
      ws.once("open", () => resolve());
      ws.once("error", reject);
    });
    const { cookies } = (await cdpCall(ws, 1, "Storage.getCookies")) as { cookies?: Array<{ name: string; domain: string; value: string }> };
    const d = cookies?.find((c) => c.name === "d" && /(^|\.)slack\.com$/.test(c.domain.replace(/^\./, "")) && decodeURIComponent(c.value).startsWith("xoxd-"));
    if (!d) throw new GabrielError("INVALID_STATE", "no signed-in `.slack.com` session cookie found in that Chrome; sign in to Slack there first");
    // Verify the cookie is live before handing it back.
    await exchangeCookie({ fetch, metadata: { ...DEFAULT_CLIENT_METADATA }, endpoints: { ...DEFAULT_ENDPOINTS } }, d.value);
    return d.value;
  } finally {
    ws.close();
  }
}
