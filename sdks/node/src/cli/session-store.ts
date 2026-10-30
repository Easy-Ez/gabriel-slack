import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, statSync, unlinkSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { GabrielError } from "../errors.js";

/**
 * Local session cookie storage used by the `gabriel` CLI: `{configDir}/auth.json`,
 * directory 0700, file 0600. Prefer an OS keychain or secret manager in production;
 * this file exists for local development convenience.
 */
export interface StoredSession {
  cookieD: string;
  workspace?: string;
  savedAt: string;
}

export function configDir(): string {
  const explicit = process.env.GABRIEL_CONFIG_DIR;
  if (explicit) return path.resolve(explicit);
  const xdg = process.env.XDG_CONFIG_HOME;
  return path.join(xdg ? path.resolve(xdg) : path.join(os.homedir(), ".config"), "gabriel");
}

export function sessionFile(): string {
  return path.join(configDir(), "auth.json");
}

export function saveSession(session: { cookieD: string; workspace?: string }): string {
  const dir = configDir();
  mkdirSync(dir, { recursive: true, mode: 0o700 });
  chmodSync(dir, 0o700);
  const file = sessionFile();
  const tmp = `${file}.${process.pid}.tmp`;
  const body: StoredSession = { cookieD: session.cookieD, ...(session.workspace ? { workspace: session.workspace } : {}), savedAt: new Date().toISOString() };
  writeFileSync(tmp, JSON.stringify(body, null, 2), { mode: 0o600 });
  chmodSync(tmp, 0o600);
  renameSync(tmp, file);
  return file;
}

export function loadSession(): StoredSession | undefined {
  const file = sessionFile();
  if (!existsSync(file)) return undefined;
  if (process.platform !== "win32") {
    const mode = statSync(file).mode & 0o777;
    if ((mode & 0o077) !== 0) {
      throw new GabrielError("PERMISSION_DENIED", `${file} is accessible by other users (mode ${mode.toString(8)}); run: chmod 600 "${file}"`);
    }
  }
  const parsed = JSON.parse(readFileSync(file, "utf8")) as Partial<StoredSession>;
  if (typeof parsed.cookieD !== "string" || parsed.cookieD.length < 8) return undefined;
  return { cookieD: parsed.cookieD, savedAt: parsed.savedAt ?? "", ...(parsed.workspace ? { workspace: parsed.workspace } : {}) };
}

export function deleteSession(): boolean {
  const file = sessionFile();
  if (!existsSync(file)) return false;
  unlinkSync(file);
  return true;
}

/** `GABRIEL_COOKIE_D` from the environment, else the CLI-saved session. */
export function loadSessionCookie(): { cookieD: string; workspace?: string } | undefined {
  const env = process.env.GABRIEL_COOKIE_D?.trim();
  if (env) return { cookieD: env, ...(process.env.GABRIEL_WORKSPACE ? { workspace: process.env.GABRIEL_WORKSPACE } : {}) };
  const stored = loadSession();
  return stored ? { cookieD: stored.cookieD, ...(stored.workspace ? { workspace: stored.workspace } : {}) } : undefined;
}

/** Extracts the `d` cookie from a browser "Copy as cURL" command. */
export function parseCurlForCookie(curl: string): string | undefined {
  const candidates: string[] = [];
  const header = /(?:-H|--header)\s+(['"])\s*cookie:\s*([\s\S]*?)\1/gi;
  for (const m of curl.matchAll(header)) candidates.push(m[2]!);
  const cookieFlag = /(?:-b|--cookie)\s+(['"])([\s\S]*?)\1/g;
  for (const m of curl.matchAll(cookieFlag)) candidates.push(m[2]!);
  for (const c of candidates) {
    for (const part of c.split(";")) {
      const [k, ...rest] = part.trim().split("=");
      if (k === "d" && rest.length) {
        const value = rest.join("=").trim();
        let decoded = value;
        try {
          decoded = decodeURIComponent(value);
        } catch {
          /* keep the raw value */
        }
        if (/^xoxd-/.test(decoded)) return value;
      }
    }
  }
  return undefined;
}
