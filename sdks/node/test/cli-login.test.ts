import { mkdtempSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const COOKIE = "xoxd-test-cookie-value";
const TOKEN = "xoxc-test-token-1";

const signInWithBrowser = vi.fn();
vi.mock("../src/core/sign-in.js", () => ({ signInWithBrowser }));

const { main } = await import("../src/cli/gabriel.js");
const { loadSession } = await import("../src/cli/session-store.js");

describe("gabriel auth login", () => {
  const saved: Record<string, string | undefined> = {};
  let output = "";

  beforeEach(() => {
    for (const key of ["GABRIEL_CONFIG_DIR", "GABRIEL_CHROME_PATH"]) saved[key] = process.env[key];
    process.env.GABRIEL_CONFIG_DIR = mkdtempSync(path.join(os.tmpdir(), "gabriel-cli-"));
    output = "";
    const capture = (chunk: string | Uint8Array) => {
      output += String(chunk);
      return true;
    };
    vi.spyOn(process.stdout, "write").mockImplementation(capture);
    vi.spyOn(process.stderr, "write").mockImplementation(capture);
    vi.stubGlobal("fetch", async () =>
      Response.json({
        teams: { T0123456789: { id: "T0123456789", name: "Your Workspace", domain: "your-workspace", token: TOKEN, user_id: "U0ALICE0001" } },
      }),
    );
    signInWithBrowser.mockReset();
  });

  afterEach(() => {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("signs in through signInWithBrowser and saves the session", async () => {
    process.env.GABRIEL_CHROME_PATH = "/opt/chromium/chrome";
    signInWithBrowser.mockImplementation(async (opts: { onStatus?: (s: string) => void }) => {
      for (const s of ["opening", "waiting", "verifying", "done"]) opts.onStatus?.(s);
      return { cookieD: COOKIE, workspaces: [{ teamId: "T0123456789", domain: "your-workspace", name: "Your Workspace", userId: "U0ALICE0001" }] };
    });

    expect(await main(["auth", "login", "your-workspace"])).toBe(0);

    expect(signInWithBrowser).toHaveBeenCalledWith(expect.objectContaining({ workspace: "your-workspace", browserPath: "/opt/chromium/chrome" }));
    expect(loadSession()).toMatchObject({ cookieD: COOKIE, workspace: "your-workspace" });
    expect(statSync(path.join(process.env.GABRIEL_CONFIG_DIR!, "auth.json")).mode & 0o777).toBe(0o600);
    expect(output).toContain("Opening a sign-in window");
    expect(output).toContain("your-workspace (T0123456789) user=U0ALICE0001");
    expect(output).not.toContain(COOKIE);
    expect(output).not.toContain(TOKEN);
  });

  it("saves nothing when the sign-in fails", async () => {
    const { GabrielError } = await import("../src/errors.js");
    signInWithBrowser.mockRejectedValue(new GabrielError("SIGN_IN_TIMEOUT", "sign-in did not complete within 300 s"));
    await expect(main(["auth", "login"])).rejects.toMatchObject({ code: "SIGN_IN_TIMEOUT" });
    expect(signInWithBrowser).toHaveBeenCalledWith(expect.not.objectContaining({ workspace: expect.anything() }));
    expect(loadSession()).toBeUndefined();
  });
});
