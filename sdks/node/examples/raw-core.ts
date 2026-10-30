/**
 * Escape hatch: log every raw frame type and call an unwrapped Web API method.
 * Raw access bypasses loop guards, the scope sandbox and origin detection.
 */
import { GabrielClient, loadSessionCookie } from "../src/index.js";

const session = loadSessionCookie();
if (!session) throw new Error("Set GABRIEL_COOKIE_D first.");
const client = GabrielClient.fromCookie(session);

client.core.onRawFrame((frame) => {
  if (frame.type !== "pong") console.log("frame", frame.type, frame.subtype ?? "");
});
client.on("guard.protocol_drift", (e) => console.warn("unknown frame type", e.unknownType, e.unknownSubtype ?? ""));

await client.start();
const prefs = await client.core.callWebApi("users.prefs.get", {});
console.log("prefs keys:", Object.keys((prefs as { prefs?: object }).prefs ?? {}).length);
