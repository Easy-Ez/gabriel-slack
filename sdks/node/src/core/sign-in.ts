import { spawn as nodeSpawn } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import WebSocket from "ws";
import { cancelledError, GabrielError } from "../errors.js";
import { anySignal, sleep, withTimeout } from "../util/async.js";
import { exchangeCookie, findTeam, type AuthTeam } from "./auth.js";
import { DEFAULT_CLIENT_METADATA, DEFAULT_ENDPOINTS, type ClientMetadata, type Endpoints } from "./metadata.js";
import type { FetchLike } from "./types.js";

/** A workspace the session cookie is signed in to. Carries no credentials. */
export interface WorkspaceInfo {
  teamId: string;
  domain: string;
  name: string;
  userId: string;
}

/** Progress of {@link signInWithBrowser}. `verifying` and `waiting` may alternate during SSO redirects. */
export type SignInStatus = "opening" | "waiting" | "verifying" | "done";

export interface SignInOptions {
  /** Workspace subdomain (`your-workspace`): the window opens on its sign-in page. Default: `https://slack.com/signin`. */
  workspace?: string;
  /** Limit for the whole sign-in. Default 300 000 ms. */
  timeoutMs?: number;
  /** Aborting closes the window and rejects with `CANCELLED`. */
  signal?: AbortSignal;
  /** Absolute path of a Chrome / Chromium executable. Default: the first one found in the usual install locations. */
  browserPath?: string;
  /** Progress callback. The only output of the sign-in; exceptions it throws are ignored. */
  onStatus?: (status: SignInStatus) => void;
}

export interface SignInResult {
  /** The `.slack.com` `d` session cookie (`xoxd-…`). Treat it like a password. */
  cookieD: string;
  workspaces: WorkspaceInfo[];
}

/** Minimal child-process surface of the sign-in window (matches `ChildProcess`). */
export interface BrowserProcess {
  kill(signal?: NodeJS.Signals): boolean;
  on(event: "exit", listener: () => void): unknown;
  on(event: "error", listener: (err: Error) => void): unknown;
}

/** Minimal DevTools WebSocket surface (matches the `ws` package). */
export interface CdpSocket {
  send(data: string): void;
  close(): void;
  on(event: "message", listener: (data: unknown) => void): unknown;
  off(event: "message", listener: (data: unknown) => void): unknown;
  on(event: "error", listener: (err: Error) => void): unknown;
  once(event: "open" | "close", listener: () => void): unknown;
}

/** Side effects of the sign-in, injectable for tests. */
export interface SignInDeps {
  fetch: FetchLike;
  metadata: ClientMetadata;
  endpoints: Endpoints;
  spawn(command: string, args: string[]): BrowserProcess;
  createWebSocket(url: string): CdpSocket;
  fs: {
    exists(file: string): boolean;
    /** Creates a fresh private temporary directory whose name starts with `prefix`. */
    mkdtemp(prefix: string): string;
    /** File contents, or `undefined` when it does not exist (yet). */
    readFile(file: string): string | undefined;
    rm(dir: string): void;
  };
  /** Cookie poll interval. */
  pollIntervalMs: number;
  /** How long a signed-in-looking cookie may keep failing the exchange (SSO redirects) before it counts as invalid. */
  rejectedGraceMs: number;
  /** Limit for the browser to start and accept the DevTools connection. */
  launchTimeoutMs: number;
  /** How long to wait for the browser to exit after SIGTERM before SIGKILL. */
  exitWaitMs: number;
}

const CHROME_CANDIDATES = [
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/Applications/Chromium.app/Contents/MacOS/Chromium",
  "/usr/bin/google-chrome",
  "/usr/bin/google-chrome-stable",
  "/usr/bin/chromium",
  "/usr/bin/chromium-browser",
];

const DEFAULT_SIGN_IN_URL = "https://slack.com/signin";
const CDP_CALL_TIMEOUT_MS = 10_000;
const EXCHANGE_TIMEOUT_MS = 15_000;

function defaultSignInDeps(): SignInDeps {
  return {
    fetch: (input, init) => fetch(input, init),
    metadata: { ...DEFAULT_CLIENT_METADATA },
    endpoints: { ...DEFAULT_ENDPOINTS },
    spawn: (command, args) => nodeSpawn(command, args, { stdio: "ignore" }),
    createWebSocket: (url) => new WebSocket(url, { perMessageDeflate: false }),
    fs: {
      exists: existsSync,
      mkdtemp: (prefix) => mkdtempSync(path.join(os.tmpdir(), prefix)),
      readFile: (file) => {
        try {
          return readFileSync(file, "utf8");
        } catch {
          return undefined;
        }
      },
      rm: (dir) => rmSync(dir, { recursive: true, force: true, maxRetries: 3, retryDelay: 200 }),
    },
    pollIntervalMs: 1000,
    rejectedGraceMs: 30_000,
    launchTimeoutMs: 15_000,
    exitWaitMs: 3000,
  };
}

function toWorkspaceInfo(team: AuthTeam): WorkspaceInfo {
  return { teamId: team.id, domain: team.domain, name: team.name, userId: team.userId };
}

/**
 * Lists the workspaces a session cookie is signed in to. Verifies the cookie with one
 * exchange; the workspace tokens it yields are dropped, never returned or logged.
 * Rejects with `AUTH_DEAD` when the cookie is no longer signed in.
 */
export function listWorkspaces(cookieD: string, opts: { signal?: AbortSignal } = {}): Promise<WorkspaceInfo[]> {
  return listWorkspacesWith(defaultSignInDeps(), cookieD, opts);
}

/** {@link listWorkspaces} with injected dependencies. */
export async function listWorkspacesWith(
  deps: Pick<SignInDeps, "fetch" | "metadata" | "endpoints">,
  cookieD: string,
  opts: { signal?: AbortSignal } = {},
): Promise<WorkspaceInfo[]> {
  if (typeof cookieD !== "string" || cookieD.trim().length < 8) {
    throw new GabrielError("INVALID_ARGUMENT", "cookieD is required (the `d` session cookie)");
  }
  if (opts.signal?.aborted) throw cancelledError(opts.signal.reason);
  try {
    const { teams } = await exchangeCookie(deps, cookieD, anySignal(opts.signal, AbortSignal.timeout(EXCHANGE_TIMEOUT_MS)));
    return teams.map(toWorkspaceInfo);
  } catch (err) {
    if (opts.signal?.aborted) throw cancelledError(opts.signal.reason);
    throw err;
  }
}

/**
 * Signs in to Slack in a dedicated Chrome window (fresh temporary profile). The person
 * signs in themselves; once the `.slack.com` `d` cookie exchanges for a workspace token,
 * the window closes and the cookie is returned with the signed-in workspaces.
 *
 * Writes nothing to disk besides the temporary profile, which is deleted whatever the
 * outcome, and prints nothing: progress is reported through `onStatus` only. Errors never
 * contain the cookie or a token.
 *
 * Rejects with `BROWSER_NOT_FOUND` (no Chrome / Chromium), `SIGN_IN_TIMEOUT`, `CANCELLED`
 * (`signal` aborted or the window was closed) or `AUTH_DEAD` (signed in, but Slack rejects
 * the session cookie).
 */
export function signInWithBrowser(opts: SignInOptions = {}): Promise<SignInResult> {
  return signInWithBrowserWith(defaultSignInDeps(), opts);
}

function resolveBrowser(browserPath: string | undefined, exists: (file: string) => boolean): string {
  if (browserPath !== undefined) {
    if (typeof browserPath !== "string" || !path.isAbsolute(browserPath)) {
      throw new GabrielError("INVALID_ARGUMENT", "browserPath must be an absolute path");
    }
    if (!exists(browserPath)) throw new GabrielError("BROWSER_NOT_FOUND", "browserPath does not exist");
    return browserPath;
  }
  const found = CHROME_CANDIDATES.find((p) => exists(p));
  if (!found) throw new GabrielError("BROWSER_NOT_FOUND", "Chrome or Chromium not found; pass browserPath");
  return found;
}

async function signInUrl(deps: SignInDeps, workspace: string | undefined, signal: AbortSignal): Promise<string> {
  if (!workspace) return DEFAULT_SIGN_IN_URL;
  const team = await findTeam(deps, workspace, signal).catch(() => undefined);
  if (team?.ok === true && typeof team.url === "string" && /^https:\/\/[a-z0-9-]+\.slack\.com\/?$/i.test(team.url)) return team.url;
  return DEFAULT_SIGN_IN_URL;
}

/** Reads the DevTools endpoint Chrome writes into the profile when started with `--remote-debugging-port=0`. */
async function waitForDevTools(deps: SignInDeps, profile: string, signal: AbortSignal): Promise<string> {
  const deadline = Date.now() + deps.launchTimeoutMs;
  while (Date.now() < deadline) {
    const [port, wsPath] = (deps.fs.readFile(path.join(profile, "DevToolsActivePort")) ?? "").split("\n").map((l) => l.trim());
    if (port && /^\d{1,5}$/.test(port) && Number(port) <= 65535 && wsPath && /^\/devtools\/browser\/[\w-]+$/.test(wsPath)) {
      return `ws://127.0.0.1:${port}${wsPath}`;
    }
    await sleep(100, signal);
  }
  throw new GabrielError("INVALID_STATE", "could not reach the sign-in browser window");
}

function connectDevTools(deps: SignInDeps, url: string): { socket: CdpSocket; opened: Promise<void> } {
  const socket = deps.createWebSocket(url);
  const opened = withTimeout(
    new Promise<void>((resolve, reject) => {
      socket.once("open", () => resolve());
      // Stays registered: an unhandled `error` would crash the process; a lost connection also emits `close`.
      socket.on("error", (err) => reject(new GabrielError("INVALID_STATE", "could not reach the sign-in browser window", { cause: err })));
    }),
    deps.launchTimeoutMs,
    () => new GabrielError("INVALID_STATE", "could not reach the sign-in browser window"),
  );
  return { socket, opened };
}

function cdpClient(socket: CdpSocket): (method: string) => Promise<Record<string, unknown>> {
  let seq = 0;
  return (method) =>
    new Promise((resolve, reject) => {
      const id = ++seq;
      const timer = setTimeout(() => {
        socket.off("message", onMessage);
        reject(new GabrielError("INVALID_STATE", `the sign-in browser window did not answer ${method}`));
      }, CDP_CALL_TIMEOUT_MS);
      const onMessage = (raw: unknown) => {
        let msg: { id?: number; error?: { message?: string }; result?: Record<string, unknown> };
        try {
          msg = JSON.parse(String(raw)) as typeof msg;
        } catch {
          return;
        }
        if (msg.id !== id) return;
        clearTimeout(timer);
        socket.off("message", onMessage);
        if (msg.error) reject(new GabrielError("INVALID_STATE", `the sign-in browser window rejected ${method}`));
        else resolve(msg.result ?? {});
      };
      socket.on("message", onMessage);
      socket.send(JSON.stringify({ id, method }));
    });
}

function sessionCookie(cookies: unknown): string | undefined {
  if (!Array.isArray(cookies)) return undefined;
  for (const c of cookies as Array<{ name?: unknown; domain?: unknown; value?: unknown }>) {
    if (c.name !== "d" || typeof c.domain !== "string" || typeof c.value !== "string") continue;
    if (!/(^|\.)slack\.com$/.test(c.domain.replace(/^\./, ""))) continue;
    let decoded = c.value;
    try {
      decoded = decodeURIComponent(c.value);
    } catch {
      /* keep the raw value */
    }
    if (decoded.startsWith("xoxd-")) return c.value;
  }
  return undefined;
}

const windowClosed = () => new GabrielError("CANCELLED", "the sign-in window was closed before signing in");
const cookieRejected = () => new GabrielError("AUTH_DEAD", "signed in, but Slack rejected the session cookie");

/** {@link signInWithBrowser} with injected dependencies. */
export async function signInWithBrowserWith(deps: SignInDeps, opts: SignInOptions = {}): Promise<SignInResult> {
  const timeoutMs = opts.timeoutMs ?? 300_000;
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new GabrielError("INVALID_ARGUMENT", "timeoutMs must be a positive number");
  if (opts.workspace !== undefined && (typeof opts.workspace !== "string" || opts.workspace.length > 255)) {
    throw new GabrielError("INVALID_ARGUMENT", "workspace must be a workspace subdomain like \"your-workspace\"");
  }
  if (opts.signal?.aborted) throw cancelledError(opts.signal.reason);
  const browser = resolveBrowser(opts.browserPath, deps.fs.exists);

  let lastStatus: SignInStatus | undefined;
  const report = (status: SignInStatus) => {
    if (status === lastStatus) return;
    lastStatus = status;
    try {
      opts.onStatus?.(status);
    } catch {
      /* a failing progress callback must not break the sign-in */
    }
  };

  // Every way the sign-in can end early (abort, timeout, window closed, launch failure)
  // aborts `stop` with its error; each await below races against it.
  const stop = new AbortController();
  let closing = false;
  const fail = (err: GabrielError) => {
    if (!closing && !stop.signal.aborted) stop.abort(err);
  };
  const stopped = new Promise<never>((_, reject) => stop.signal.addEventListener("abort", () => reject(stop.signal.reason), { once: true }));
  stopped.catch(() => undefined);
  const guard = <T>(p: Promise<T>): Promise<T> => Promise.race([p, stopped]);

  let rejected: { cookie: string; since: number } | undefined;
  const onAbort = () => fail(cancelledError(opts.signal?.reason));
  opts.signal?.addEventListener("abort", onAbort, { once: true });
  const timer = setTimeout(
    () => fail(rejected ? cookieRejected() : new GabrielError("SIGN_IN_TIMEOUT", `sign-in did not complete within ${Math.round(timeoutMs / 1000)} s`)),
    timeoutMs,
  );

  let profile: string | undefined;
  let proc: BrowserProcess | undefined;
  let exited = false;
  let socket: CdpSocket | undefined;
  let result: SignInResult | undefined;
  try {
    report("opening");
    const loginUrl = await guard(signInUrl(deps, opts.workspace, stop.signal));
    profile = deps.fs.mkdtemp("gabriel-login-");
    proc = deps.spawn(browser, [
      "--remote-debugging-port=0",
      `--user-data-dir=${profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      `--app=${loginUrl}`,
    ]);
    proc.on("exit", () => {
      exited = true;
      fail(windowClosed());
    });
    proc.on("error", (err) => {
      exited = true;
      const code = (err as NodeJS.ErrnoException).code;
      fail(
        code === "ENOENT" || code === "EACCES"
          ? new GabrielError("BROWSER_NOT_FOUND", "the browser could not be started", { cause: err })
          : new GabrielError("INVALID_STATE", "the browser could not be started", { cause: err }),
      );
    });
    const wsUrl = await guard(waitForDevTools(deps, profile, stop.signal));
    const conn = connectDevTools(deps, wsUrl);
    socket = conn.socket;
    await guard(conn.opened);
    socket.once("close", () => fail(windowClosed()));
    const cdp = cdpClient(socket);

    report("waiting");
    let sawWindow = false;
    for (;;) {
      // On macOS closing the last window leaves Chrome running, so watch the targets too.
      const { targetInfos } = await guard(cdp("Target.getTargets"));
      const windows = Array.isArray(targetInfos) ? targetInfos.filter((t: { type?: unknown }) => t.type === "page" || t.type === "app") : [];
      if (windows.length > 0) sawWindow = true;
      else if (sawWindow) throw windowClosed();

      const { cookies } = await guard(cdp("Storage.getCookies"));
      const cookieD = sessionCookie(cookies);
      if (cookieD) {
        report("verifying");
        try {
          const { teams } = await guard(exchangeCookie(deps, cookieD, anySignal(stop.signal, AbortSignal.timeout(EXCHANGE_TIMEOUT_MS))));
          result = { cookieD, workspaces: teams.map(toWorkspaceInfo) };
          break;
        } catch (err) {
          if (stop.signal.aborted) throw stop.signal.reason;
          if (err instanceof GabrielError && err.code === "AUTH_DEAD") {
            // Usually the sign-in is still redirecting through SSO; give it time before giving up.
            if (rejected?.cookie !== cookieD) rejected = { cookie: cookieD, since: Date.now() };
            else if (Date.now() - rejected.since >= deps.rejectedGraceMs) throw cookieRejected();
          } else if (!(err instanceof GabrielError && err.retryable)) {
            throw err;
          }
        }
        report("waiting");
      } else {
        rejected = undefined;
      }
      await sleep(deps.pollIntervalMs, stop.signal);
    }
  } catch (err) {
    if (stop.signal.aborted) throw stop.signal.reason;
    if (err instanceof GabrielError) throw err;
    throw new GabrielError("INVALID_STATE", "browser sign-in failed", { cause: err });
  } finally {
    closing = true;
    clearTimeout(timer);
    opts.signal?.removeEventListener("abort", onAbort);
    await closeWindow(deps, proc, () => exited, socket);
    if (profile) {
      try {
        deps.fs.rm(profile);
      } catch {
        /* best effort; the OS cleans its temp directory eventually */
      }
    }
  }
  report("done");
  return result!;
}

async function closeWindow(deps: SignInDeps, proc: BrowserProcess | undefined, exited: () => boolean, socket: CdpSocket | undefined): Promise<void> {
  try {
    socket?.close();
  } catch {
    /* already closed */
  }
  if (!proc || exited()) return;
  const gone = new Promise<void>((resolve) => proc.on("exit", () => resolve()));
  const kill = (signal: NodeJS.Signals) => {
    try {
      proc.kill(signal);
    } catch {
      /* already gone */
    }
  };
  kill("SIGTERM");
  try {
    await withTimeout(gone, deps.exitWaitMs, () => new Error("browser did not exit"));
  } catch {
    kill("SIGKILL");
    await withTimeout(gone, deps.exitWaitMs, () => new Error("browser did not exit")).catch(() => undefined);
  }
}
