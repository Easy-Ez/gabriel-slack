import { mkdtempSync, statSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { loadSession, parseCurlForCookie, saveSession } from "../src/cli/session-store.js";
import { FileStateStore } from "../src/state/file.js";

describe("parseCurlForCookie", () => {
  it("finds the d cookie in -H and -b forms", () => {
    expect(parseCurlForCookie(`curl 'https://x.slack.com/api/a' -H 'cookie: b=1; d=xoxd-abc%2Bdef; d-s=1'`)).toBe("xoxd-abc%2Bdef");
    expect(parseCurlForCookie(`curl "https://x" -b "lc=1; d=xoxd-zzz"`)).toBe("xoxd-zzz");
    expect(parseCurlForCookie(`curl "https://x" -H "Cookie: other=1"`)).toBeUndefined();
  });
});

describe("session store", () => {
  let prev: string | undefined;
  beforeEach(() => {
    prev = process.env.GABRIEL_CONFIG_DIR;
    process.env.GABRIEL_CONFIG_DIR = mkdtempSync(path.join(os.tmpdir(), "gabriel-cli-"));
  });
  afterEach(() => {
    if (prev === undefined) delete process.env.GABRIEL_CONFIG_DIR;
    else process.env.GABRIEL_CONFIG_DIR = prev;
  });

  it("writes 0600 and reads back", () => {
    const file = saveSession({ cookieD: "xoxd-test-cookie-value", workspace: "your-workspace" });
    expect(statSync(file).mode & 0o777).toBe(0o600);
    expect(loadSession()).toMatchObject({ cookieD: "xoxd-test-cookie-value", workspace: "your-workspace" });
  });
});

describe("FileStateStore", () => {
  it("persists across instances with 0600 permissions", async () => {
    const dir = mkdtempSync(path.join(os.tmpdir(), "gabriel-state-"));
    const a = new FileStateStore({ dir });
    await a.maxMergeTs("wm", "1800000001.000100");
    await a.close();
    expect(statSync(path.join(dir, "state.json")).mode & 0o777).toBe(0o600);
    const b = new FileStateStore({ dir });
    expect(await b.get("wm")).toBe("1800000001.000100");
    await b.close();
  });
});
