import { EventEmitter } from "node:events";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { listWorkspacesWith, signInWithBrowserWith, type SignInDeps, type SignInStatus } from "../src/core/sign-in.js";
import { GabrielError } from "../src/errors.js";

const CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";
const COOKIE = "xoxd-test-cookie-value";
const TOKEN = "xoxc-test-token-1";
const ENDPOINTS = {
  authUrl: "https://app.slack.test/auth?app=client&response_type=json",
  publicApiBase: "https://slack.test/api/",
  edgeApiBase: "https://edgeapi.slack.test/cache/",
  wsBase: "wss://ws.slack.test/",
};
const WORKSPACE = { teamId: "T0123456789", domain: "your-workspace", name: "Your Workspace", userId: "U0ALICE0001" };

class FakeProcess extends EventEmitter {
  readonly signals: string[] = [];
  ignoreSigterm = false;

  kill(signal?: NodeJS.Signals): boolean {
    this.signals.push(signal ?? "SIGTERM");
    if (signal === "SIGTERM" && this.ignoreSigterm) return true;
    setImmediate(() => this.emit("exit", null, signal));
    return true;
  }
}

/** Chrome's browser-level DevTools endpoint: answers `Target.getTargets` and `Storage.getCookies`. */
class FakeDevTools extends EventEmitter {
  closed = false;
  readonly methods: string[] = [];

  constructor(
    readonly url: string,
    private readonly browser: FakeBrowser,
  ) {
    super();
    setImmediate(() => this.emit("open"));
  }

  send(data: string): void {
    const { id, method } = JSON.parse(data) as { id: number; method: string };
    this.methods.push(method);
    const result =
      method === "Target.getTargets"
        ? { targetInfos: Array.from({ length: this.browser.windows }, () => ({ type: "page" })) }
        : { cookies: this.browser.cookies() };
    setImmediate(() => this.emit("message", Buffer.from(JSON.stringify({ id, result }))));
  }

  close(): void {
    if (this.closed) return;
    this.closed = true;
    setImmediate(() => this.emit("close"));
  }
}

/** In-memory browser, file system and Slack endpoints for the sign-in flow. */
class FakeBrowser {
  readonly installed = new Set([CHROME]);
  readonly files = new Map<string, string>();
  readonly profiles: string[] = [];
  readonly removed: string[] = [];
  readonly spawns: Array<{ command: string; args: string[] }> = [];
  readonly sockets: FakeDevTools[] = [];
  readonly authCookies: string[] = [];
  process: FakeProcess | undefined;
  windows = 1;
  /** Cookie polls before the `d` cookie appears; `Infinity` never signs in. */
  signInAfterPolls = 2;
  /** Cookie exchanges answered with a sign-in redirect before succeeding; `Infinity` keeps rejecting. */
  rejectExchanges = 0;
  writeDevToolsPort = true;
  spawnError: NodeJS.ErrnoException | undefined;
  private polls = 0;

  cookies(): Array<{ name: string; domain: string; value: string }> {
    const base = [{ name: "b", domain: ".slack.com", value: "anonymous" }];
    return ++this.polls > this.signInAfterPolls ? [...base, { name: "d", domain: ".slack.com", value: COOKIE }] : base;
  }

  get profile(): string {
    return this.profiles[0]!;
  }

  readonly fetch = async (input: string | URL | Request, init?: RequestInit): Promise<Response> => {
    const url = new URL(typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url);
    if (url.hostname === "slack.test" && url.pathname === "/api/auth.findTeam") {
      return Response.json({ ok: true, url: "https://your-workspace.slack.com/" });
    }
    if (url.hostname === "app.slack.test") {
      this.authCookies.push(new Headers(init?.headers).get("cookie") ?? "");
      if (this.rejectExchanges-- > 0) return new Response("", { status: 302, headers: { location: "https://slack.test/signin" } });
      return Response.json({
        teams: { T0123456789: { id: "T0123456789", name: "Your Workspace", domain: "your-workspace", url: "https://your-workspace.slack.com/", token: TOKEN, user_id: "U0ALICE0001" } },
      });
    }
    throw new Error(`unexpected fetch ${url.toString()}`);
  };

  deps(overrides: Partial<SignInDeps> = {}): SignInDeps {
    return {
      fetch: this.fetch as typeof fetch,
      metadata: { userAgent: "test-agent", agentVersion: "1", origin: "https://app.slack.test" },
      endpoints: ENDPOINTS,
      spawn: (command, args) => {
        this.spawns.push({ command, args });
        const proc = new FakeProcess();
        this.process = proc;
        if (this.spawnError) {
          const err = this.spawnError;
          setImmediate(() => proc.emit("error", err));
        } else if (this.writeDevToolsPort) {
          const profile = args.find((a) => a.startsWith("--user-data-dir="))!.slice("--user-data-dir=".length);
          setTimeout(() => this.files.set(path.join(profile, "DevToolsActivePort"), "51234\n/devtools/browser/0a1b2c3d-test\n"), 5);
        }
        return proc;
      },
      createWebSocket: (url) => {
        const socket = new FakeDevTools(url, this);
        this.sockets.push(socket);
        return socket;
      },
      fs: {
        exists: (file) => this.installed.has(file),
        mkdtemp: (prefix) => {
          const dir = path.join("/tmp", `${prefix}${this.profiles.length}`);
          this.profiles.push(dir);
          return dir;
        },
        readFile: (file) => this.files.get(file),
        rm: (dir) => {
          this.removed.push(dir);
        },
      },
      pollIntervalMs: 5,
      rejectedGraceMs: 60_000,
      launchTimeoutMs: 500,
      exitWaitMs: 50,
      ...overrides,
    };
  }
}

async function rejection(p: Promise<unknown>): Promise<GabrielError> {
  try {
    await p;
  } catch (err) {
    expect(err).toBeInstanceOf(GabrielError);
    return err as GabrielError;
  }
  throw new Error("expected a rejection");
}

function expectNoSecrets(...texts: string[]): void {
  for (const text of texts) {
    expect(text).not.toContain(COOKIE);
    expect(text).not.toContain(TOKEN);
  }
}

describe("signInWithBrowser", () => {
  it("returns the cookie and workspaces, closes the window and removes the profile", async () => {
    const browser = new FakeBrowser();
    const statuses: SignInStatus[] = [];
    const result = await signInWithBrowserWith(browser.deps(), { workspace: "your-workspace", onStatus: (s) => statuses.push(s) });

    expect(result).toEqual({ cookieD: COOKIE, workspaces: [WORKSPACE] });
    expectNoSecrets(JSON.stringify(result.workspaces));
    expect(statuses).toEqual(["opening", "waiting", "verifying", "done"]);

    const [spawned] = browser.spawns;
    expect(spawned?.command).toBe(CHROME);
    expect(spawned?.args).toEqual([
      "--remote-debugging-port=0",
      `--user-data-dir=${browser.profile}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--app=https://your-workspace.slack.com/",
    ]);
    expect(browser.sockets[0]?.url).toBe("ws://127.0.0.1:51234/devtools/browser/0a1b2c3d-test");
    expect(browser.sockets[0]?.closed).toBe(true);
    expect(browser.process?.signals).toEqual(["SIGTERM"]);
    expect(browser.removed).toEqual([browser.profile]);
    expect(browser.authCookies).toEqual([`d=${COOKIE}`]);
  });

  it("opens the generic sign-in page without a workspace and uses browserPath", async () => {
    const browser = new FakeBrowser();
    browser.installed.clear();
    browser.installed.add("/opt/chromium/chrome");
    await signInWithBrowserWith(browser.deps(), { browserPath: "/opt/chromium/chrome" });
    expect(browser.spawns[0]?.command).toBe("/opt/chromium/chrome");
    expect(browser.spawns[0]?.args.at(-1)).toBe("--app=https://slack.com/signin");
  });

  it("keeps waiting while the cookie is still redirecting through SSO", async () => {
    const browser = new FakeBrowser();
    browser.rejectExchanges = 2;
    const statuses: SignInStatus[] = [];
    const result = await signInWithBrowserWith(browser.deps(), { onStatus: (s) => statuses.push(s) });
    expect(result.workspaces).toEqual([WORKSPACE]);
    expect(browser.authCookies).toHaveLength(3);
    expect(statuses).toEqual(["opening", "waiting", "verifying", "waiting", "verifying", "waiting", "verifying", "done"]);
  });

  it("times out with SIGN_IN_TIMEOUT and cleans up", async () => {
    const browser = new FakeBrowser();
    browser.signInAfterPolls = Infinity;
    const err = await rejection(signInWithBrowserWith(browser.deps(), { timeoutMs: 300 }));
    expect(err.code).toBe("SIGN_IN_TIMEOUT");
    expect(browser.process?.signals).toEqual(["SIGTERM"]);
    expect(browser.sockets[0]?.closed).toBe(true);
    expect(browser.removed).toEqual([browser.profile]);
  });

  it("aborts with CANCELLED, closes the window and cleans up", async () => {
    const browser = new FakeBrowser();
    browser.signInAfterPolls = Infinity;
    const ctrl = new AbortController();
    const pending = signInWithBrowserWith(browser.deps(), {
      signal: ctrl.signal,
      onStatus: (s) => {
        if (s === "waiting") setTimeout(() => ctrl.abort(), 20);
      },
    });
    const err = await rejection(pending);
    expect(err.code).toBe("CANCELLED");
    expect(browser.process?.signals).toEqual(["SIGTERM"]);
    expect(browser.removed).toEqual([browser.profile]);
  });

  it("rejects an already aborted signal without opening a window", async () => {
    const browser = new FakeBrowser();
    const ctrl = new AbortController();
    ctrl.abort();
    const err = await rejection(signInWithBrowserWith(browser.deps(), { signal: ctrl.signal }));
    expect(err.code).toBe("CANCELLED");
    expect(browser.spawns).toHaveLength(0);
    expect(browser.profiles).toHaveLength(0);
  });

  it("reports CANCELLED when the person closes the window", async () => {
    const browser = new FakeBrowser();
    browser.signInAfterPolls = Infinity;
    const pending = signInWithBrowserWith(browser.deps(), {
      onStatus: (s) => {
        if (s === "waiting") setTimeout(() => (browser.windows = 0), 20);
      },
    });
    const err = await rejection(pending);
    expect(err.code).toBe("CANCELLED");
    expect(browser.process?.signals).toEqual(["SIGTERM"]);
    expect(browser.removed).toEqual([browser.profile]);
  });

  it("reports CANCELLED when the browser exits", async () => {
    const browser = new FakeBrowser();
    browser.signInAfterPolls = Infinity;
    const pending = signInWithBrowserWith(browser.deps(), {
      onStatus: (s) => {
        if (s === "waiting") setTimeout(() => browser.process?.emit("exit", 0, null), 20);
      },
    });
    const err = await rejection(pending);
    expect(err.code).toBe("CANCELLED");
    expect(browser.process?.signals).toEqual([]);
    expect(browser.removed).toEqual([browser.profile]);
  });

  it("fails with BROWSER_NOT_FOUND when no browser is installed", async () => {
    const browser = new FakeBrowser();
    browser.installed.clear();
    const statuses: SignInStatus[] = [];
    const err = await rejection(signInWithBrowserWith(browser.deps(), { onStatus: (s) => statuses.push(s) }));
    expect(err.code).toBe("BROWSER_NOT_FOUND");
    expect(statuses).toEqual([]);
    expect(browser.spawns).toHaveLength(0);
    expect(browser.profiles).toHaveLength(0);
  });

  it("fails with BROWSER_NOT_FOUND for a missing browserPath and rejects relative paths", async () => {
    const browser = new FakeBrowser();
    expect((await rejection(signInWithBrowserWith(browser.deps(), { browserPath: "/opt/missing/chrome" }))).code).toBe("BROWSER_NOT_FOUND");
    expect((await rejection(signInWithBrowserWith(browser.deps(), { browserPath: "chrome" }))).code).toBe("INVALID_ARGUMENT");
    expect(browser.spawns).toHaveLength(0);
  });

  it("fails with BROWSER_NOT_FOUND when the browser cannot be started, and cleans up", async () => {
    const browser = new FakeBrowser();
    browser.spawnError = Object.assign(new Error("spawn ENOENT"), { code: "ENOENT" });
    const err = await rejection(signInWithBrowserWith(browser.deps()));
    expect(err.code).toBe("BROWSER_NOT_FOUND");
    expect(browser.removed).toEqual([browser.profile]);
  });

  it("fails with INVALID_STATE when the DevTools endpoint never appears, and cleans up", async () => {
    const browser = new FakeBrowser();
    browser.writeDevToolsPort = false;
    const err = await rejection(signInWithBrowserWith(browser.deps({ launchTimeoutMs: 50 })));
    expect(err.code).toBe("INVALID_STATE");
    expect(browser.process?.signals).toEqual(["SIGTERM"]);
    expect(browser.removed).toEqual([browser.profile]);
  });

  it("fails with AUTH_DEAD when the signed-in cookie keeps being rejected", async () => {
    const browser = new FakeBrowser();
    browser.rejectExchanges = Infinity;
    const err = await rejection(signInWithBrowserWith(browser.deps({ rejectedGraceMs: 30 })));
    expect(err.code).toBe("AUTH_DEAD");
    expectNoSecrets(err.message, String(err.stack));
    expect(browser.removed).toEqual([browser.profile]);
  });

  it("reports AUTH_DEAD instead of a timeout when the last cookie seen was rejected", async () => {
    const browser = new FakeBrowser();
    browser.rejectExchanges = Infinity;
    const err = await rejection(signInWithBrowserWith(browser.deps(), { timeoutMs: 300 }));
    expect(err.code).toBe("AUTH_DEAD");
    expect(browser.removed).toEqual([browser.profile]);
  });

  it("kills a browser that ignores SIGTERM", async () => {
    const browser = new FakeBrowser();
    const deps = browser.deps();
    const spawn = deps.spawn;
    deps.spawn = (command, args) => {
      const proc = spawn(command, args) as FakeProcess;
      proc.ignoreSigterm = true;
      return proc;
    };
    await signInWithBrowserWith(deps);
    expect(browser.process?.signals).toEqual(["SIGTERM", "SIGKILL"]);
    expect(browser.removed).toEqual([browser.profile]);
  });

  it("survives a DevTools socket error after connecting", async () => {
    const browser = new FakeBrowser();
    browser.signInAfterPolls = Infinity;
    const pending = signInWithBrowserWith(browser.deps(), {
      onStatus: (s) => {
        if (s !== "waiting") return;
        setTimeout(() => {
          const socket = browser.sockets[0]!;
          socket.emit("error", new Error("socket hang up"));
          socket.emit("error", new Error("socket hang up"));
          socket.close();
        }, 20);
      },
    });
    expect((await rejection(pending)).code).toBe("CANCELLED");
    expect(browser.removed).toEqual([browser.profile]);
  });

  it("ignores a throwing onStatus callback", async () => {
    const browser = new FakeBrowser();
    const result = await signInWithBrowserWith(browser.deps(), {
      onStatus: () => {
        throw new Error("ui crashed");
      },
    });
    expect(result.cookieD).toBe(COOKIE);
  });

  it("validates its options", async () => {
    const browser = new FakeBrowser();
    expect((await rejection(signInWithBrowserWith(browser.deps(), { timeoutMs: 0 }))).code).toBe("INVALID_ARGUMENT");
    expect(browser.spawns).toHaveLength(0);
  });
});

describe("listWorkspaces", () => {
  it("lists workspaces without tokens", async () => {
    const browser = new FakeBrowser();
    const workspaces = await listWorkspacesWith(browser.deps(), COOKIE);
    expect(workspaces).toEqual([WORKSPACE]);
    expectNoSecrets(JSON.stringify(workspaces));
  });

  it("throws AUTH_DEAD for a signed-out cookie", async () => {
    const browser = new FakeBrowser();
    browser.rejectExchanges = Infinity;
    const err = await rejection(listWorkspacesWith(browser.deps(), COOKIE));
    expect(err.code).toBe("AUTH_DEAD");
    expectNoSecrets(err.message);
  });

  it("throws CANCELLED when aborted", async () => {
    const browser = new FakeBrowser();
    const ctrl = new AbortController();
    const deps = browser.deps({
      fetch: ((_input: unknown, init?: RequestInit) =>
        new Promise((_, reject) => init?.signal?.addEventListener("abort", () => reject(init.signal?.reason)))) as typeof fetch,
    });
    const pending = listWorkspacesWith(deps, COOKIE, { signal: ctrl.signal });
    ctrl.abort();
    expect((await rejection(pending)).code).toBe("CANCELLED");
  });

  it("rejects a missing cookie", async () => {
    const browser = new FakeBrowser();
    expect((await rejection(listWorkspacesWith(browser.deps(), ""))).code).toBe("INVALID_ARGUMENT");
    expect(browser.authCookies).toHaveLength(0);
  });
});
