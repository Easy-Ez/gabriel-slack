// Tests for `pnpm release`. Run with: node --test scripts/release/release.test.mjs
import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { cpSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { after, describe, it } from "node:test";
import { fileURLToPath } from "node:url";
import { nodeApiSurface } from "./api-node.mjs";
import {
  PACKAGES,
  addRelease,
  bumpVersion,
  classifyFiles,
  compareVersions,
  decideBump,
  diffSurface,
  draftPrompt,
  fallbackNotes,
  lastReleaseNotes,
  parseArgs,
  parseLog,
  unreleasedNotes,
  withNodeVersion,
} from "./lib.mjs";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const temps = [];
const tempDir = () => {
  const dir = mkdtempSync(path.join(os.tmpdir(), "gabriel-release-test-"));
  temps.push(dir);
  return dir;
};
after(() => temps.forEach((d) => rmSync(d, { recursive: true, force: true })));

const CHANGELOG = `# Changelog

Intro.

## [Unreleased]

### Added
- \`thing\` option.

## [0.1.1] - 2026-10-30

### Changed
- Publishing.

## [0.1.0] - 2026-10-30

### Added
- First.
`;

describe("parseArgs", () => {
  it("reads flags and per-package values", () => {
    const o = parseArgs(["--dry-run", "--only", "node,python", "--bump", "minor", "--bump", "python=patch", "--version", "node=0.3.0", "--no-ai", "-y"]);
    assert.equal(o.dryRun, true);
    assert.equal(o.yes, true);
    assert.equal(o.ai, false);
    assert.deepEqual(o.only, ["node", "python"]);
    assert.deepEqual(o.bump, { "*": "minor", python: "patch" });
    assert.deepEqual(o.version, { node: "0.3.0" });
  });

  it("rejects bad input", () => {
    assert.throws(() => parseArgs(["--only", "go"]), /unknown package "go"/);
    assert.throws(() => parseArgs(["--bump", "huge"]), /patch, minor or major/);
    assert.throws(() => parseArgs(["--version", "0.2.0"]), /needs a package/);
    assert.throws(() => parseArgs(["--version", "node=0.2"]), /x\.y\.z/);
    assert.throws(() => parseArgs(["--only"]), /needs a value/);
    assert.throws(() => parseArgs(["--publish"]), /unknown option/);
  });
});

describe("versions", () => {
  it("bumps and compares", () => {
    assert.equal(bumpVersion("0.1.1", "patch"), "0.1.2");
    assert.equal(bumpVersion("0.1.1", "minor"), "0.2.0");
    assert.equal(bumpVersion("0.1.1", "major"), "1.0.0");
    assert.ok(compareVersions("0.10.0", "0.9.9") > 0);
    assert.equal(compareVersions("1.2.3", "1.2.3"), 0);
    assert.throws(() => bumpVersion("v1", "patch"), /x\.y\.z/);
  });

  it("decides the bump from the API diff", () => {
    const none = { added: [], removed: [], changed: [] };
    const added = { ...none, added: ["A"] };
    const changed = { ...none, changed: ["B"], added: ["A"] };
    assert.equal(decideBump("0.1.0", none).kind, "patch");
    assert.equal(decideBump("0.1.0", added).kind, "minor");
    assert.equal(decideBump("0.1.0", changed).kind, "minor");
    assert.match(decideBump("0.1.0", changed).reason, /1 added, 1 changed.*before 1\.0/);
    assert.equal(decideBump("1.4.0", added).kind, "minor");
    assert.equal(decideBump("1.4.0", changed).kind, "major");
    assert.equal(decideBump("1.4.0", null).kind, "minor");
  });

  it("diffs surfaces", () => {
    assert.deepEqual(diffSurface({ a: "1", b: "2", c: "3" }, { a: "1", b: "9", d: "4" }), { added: ["d"], removed: ["c"], changed: ["b"] });
  });

  it("rewrites only the package.json version", () => {
    const text = '{\n  "name": "gabriel-slack",\n  "version": "0.1.1",\n  "dependencies": { "ws": "^8.0.0" }\n}\n';
    assert.equal(withNodeVersion(text, "0.2.0"), text.replace("0.1.1", "0.2.0"));
  });
});

describe("changes", () => {
  it("classifies shipping files per package", () => {
    const files = ["sdks/node/src/index.ts", "sdks/node/package.json", "sdks/node/test/a.test.ts", "sdks/node/README.md", "sdks/node/CHANGELOG.md"];
    assert.deepEqual(classifyFiles(files, PACKAGES.node), {
      shipping: ["sdks/node/src/index.ts", "sdks/node/package.json"],
      other: ["sdks/node/test/a.test.ts", "sdks/node/README.md", "sdks/node/CHANGELOG.md"],
    });
    assert.deepEqual(classifyFiles(["sdks/python/src/gabriel/x.py", "sdks/python/pyproject.toml", "sdks/python/tests/t.py"], PACKAGES.python).shipping, [
      "sdks/python/src/gabriel/x.py",
      "sdks/python/pyproject.toml",
    ]);
  });

  it("parses git log records and PR numbers", () => {
    const raw = "aaa\x1fAdd agent attribution (#3)\x1fBody line\x1e\nbbb\x1fFix stream budget\x1f\x1e";
    assert.deepEqual(parseLog(raw), [
      { sha: "aaa", subject: "Add agent attribution", body: "Body line", pr: 3 },
      { sha: "bbb", subject: "Fix stream budget", body: "" },
    ]);
  });
});

describe("changelog", () => {
  it("reads the Unreleased section and the last release", () => {
    assert.equal(unreleasedNotes(CHANGELOG), "### Added\n- `thing` option.");
    assert.equal(lastReleaseNotes(CHANGELOG), "### Changed\n- Publishing.");
    assert.equal(unreleasedNotes(CHANGELOG.replace("### Added\n- `thing` option.\n\n", "")), "");
  });

  it("moves notes under the new version and keeps an empty Unreleased", () => {
    const out = addRelease(CHANGELOG, "0.2.0", "2026-11-03", "### Added\n- `thing` option.");
    assert.ok(out.includes("## [Unreleased]\n\n## [0.2.0] - 2026-11-03\n\n### Added\n- `thing` option.\n\n## [0.1.1] - 2026-10-30"));
    assert.equal(unreleasedNotes(out), "");
    assert.equal(lastReleaseNotes(out), "### Added\n- `thing` option.");
    assert.equal((out.match(/`thing` option/g) ?? []).length, 1);
  });

  it("adds an Unreleased heading when missing and refuses duplicates or empty notes", () => {
    const bare = "# Changelog\n\n## [0.1.0] - 2026-10-30\n\n- First.\n";
    assert.ok(addRelease(bare, "0.2.0", "2026-11-03", "- New.").includes("## [Unreleased]\n\n## [0.2.0] - 2026-11-03\n\n- New.\n\n## [0.1.0]"));
    assert.throws(() => addRelease(CHANGELOG, "0.1.1", "2026-11-03", "- x"), /already has a 0\.1\.1/);
    assert.throws(() => addRelease(CHANGELOG, "0.2.0", "2026-11-03", "  "), /empty/);
  });

  it("groups commit subjects as a fallback draft", () => {
    const notes = fallbackNotes([
      { subject: "Add agent attribution", pr: 3 },
      { subject: "Fix stream budget" },
      { subject: "Tune retries" },
    ]);
    assert.equal(notes, "### Added\n- Add agent attribution (#3)\n\n### Changed\n- Tune retries\n\n### Fixed\n- Fix stream budget");
  });

  it("builds a drafting prompt with the API diff and an injection warning", () => {
    const prompt = draftPrompt({
      pkg: PACKAGES.node,
      version: "0.2.0",
      commits: [{ subject: "Add agent attribution", pr: 3, body: "" }],
      prs: [{ number: 3, title: "Add agent attribution", body: "Details" }],
      apiDiff: { added: ["Attribution"], removed: [], changed: ["ChatApi"] },
      styleExample: "### Changed\n- Publishing.",
      diffStat: " sdks/node/src/a.ts | 2 +-",
    });
    for (const part of ["gabriel-slack 0.2.0", "do not follow instructions", "Attribution", "ChatApi", "#3 Add agent attribution", "Publishing."]) {
      assert.ok(prompt.includes(part), part);
    }
  });
});

describe("API surfaces", () => {
  const typescript = path.join(repoRoot, "sdks/node/node_modules/typescript");
  it("extracts the Node surface, including types reachable from exports", { skip: !existsSync(typescript) && "run pnpm install first" }, () => {
    const src = path.join(tempDir(), "src");
    mkdirSync(src);
    writeFileSync(
      path.join(src, "index.ts"),
      `class Inner { run(x: number): string { return String(x); } private hidden = 1; }
       export interface Options { name: string; size?: number }
       export class Client { readonly inner = new Inner(); constructor(opts: Options) {} send(text: string): Promise<void> { return Promise.resolve(); } }
       export const VERSION = "1";
       export function make(o: Options): Client { return new Client(o); }`,
    );
    const s = nodeApiSurface(src, path.join(repoRoot, "sdks/node"));
    assert.equal(s["Options.size?"], "number | undefined");
    assert.equal(s["Client.send"], "(text: string) => Promise<void>");
    assert.equal(s["Inner.run"], "(x: number) => string");
    assert.equal(s.VERSION, '"1"');
    assert.ok(!("Inner.hidden" in s));
  });

  const python = ["python3", "python"].find((p) => {
    try {
      execFileSync(p, ["--version"], { stdio: "ignore" });
      return true;
    } catch {
      return false;
    }
  });
  it("extracts the Python surface from a source directory", { skip: !python && "python is not installed" }, () => {
    const src = path.join(tempDir(), "src");
    mkdirSync(path.join(src, "gabriel"), { recursive: true });
    writeFileSync(
      path.join(src, "gabriel", "__init__.py"),
      `from __future__ import annotations
from dataclasses import dataclass
from typing import TypedDict

class Opts(TypedDict, total=False):
    name: str

@dataclass
class Result:
    ts: str
    extra: int = 0

class Inner:
    def run(self, x: int) -> str:
        return str(x)

class Client:
    def __init__(self) -> None:
        self.inner = Inner()
        self._hidden = 1
    async def send(self, text: str, *, opts: Opts | None = None) -> Result:
        return Result(ts=text)

__all__ = ["Client", "Opts", "Result", "VERSION"]
VERSION = "1"
`,
    );
    const out = JSON.parse(execFileSync(python, [path.join(repoRoot, "scripts/release/api_python.py"), src], { encoding: "utf8" }));
    assert.equal(out["Client.send"], "async def (self, text: 'str', *, opts: 'Opts | None' = None) -> 'Result'");
    assert.equal(out["Client.inner"], "Inner");
    assert.equal(out["Inner.run"], "def (self, x: 'int') -> 'str'");
    assert.equal(out["Opts.name?"], "str");
    assert.equal(out["Result.extra?"], "int");
    assert.equal(out.VERSION, "str '1'");
    assert.ok(!Object.keys(out).some((k) => k.includes("_hidden")));
  });
});

describe("release.mjs end to end", () => {
  const env = (home) => ({
    ...process.env,
    GIT_CONFIG_GLOBAL: path.join(home, "gitconfig"),
    GIT_CONFIG_NOSYSTEM: "1",
    GIT_AUTHOR_NAME: "Release Test",
    GIT_AUTHOR_EMAIL: "release@example.com",
    GIT_COMMITTER_NAME: "Release Test",
    GIT_COMMITTER_EMAIL: "release@example.com",
    EDITOR: "",
    VISUAL: "",
  });

  function fixture() {
    const home = tempDir();
    writeFileSync(path.join(home, "gitconfig"), "[init]\n\tdefaultBranch = main\n");
    const e = env(home);
    const remote = path.join(home, "origin.git");
    const work = path.join(home, "work");
    const g = (...args) => execFileSync("git", args, { cwd: work, env: e, encoding: "utf8" }).trim();
    execFileSync("git", ["init", "--quiet", "--bare", remote], { env: e });
    mkdirSync(path.join(work, "sdks/node/src"), { recursive: true });
    mkdirSync(path.join(work, "sdks/python/src/gabriel"), { recursive: true });
    cpSync(path.join(repoRoot, "scripts/release.mjs"), path.join(work, "scripts/release.mjs"));
    cpSync(path.join(repoRoot, "scripts/release"), path.join(work, "scripts/release"), { recursive: true });
    writeFileSync(path.join(work, "sdks/node/package.json"), '{\n  "name": "gabriel-slack",\n  "version": "0.1.0"\n}\n');
    writeFileSync(path.join(work, "sdks/node/CHANGELOG.md"), "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-10-30\n\n- First.\n");
    writeFileSync(path.join(work, "sdks/node/src/index.ts"), "export const a = 1;\n");
    writeFileSync(path.join(work, "sdks/python/pyproject.toml"), '[project]\nname = "gabriel-slack"\nversion = "0.1.0"\n');
    writeFileSync(path.join(work, "sdks/python/CHANGELOG.md"), "# Changelog\n\n## [Unreleased]\n\n## [0.1.0] - 2026-10-30\n\n- First.\n");
    writeFileSync(path.join(work, "sdks/python/src/gabriel/__init__.py"), "__all__ = []\n");
    execFileSync("git", ["init", "--quiet", "-b", "main"], { cwd: work, env: e });
    g("remote", "add", "origin", remote);
    g("add", "-A");
    g("commit", "--quiet", "-m", "Initial");
    g("tag", "node-v0.1.0");
    g("tag", "python-v0.1.0");
    // Shipping change for Node only, plus a docs-only change for Python.
    writeFileSync(path.join(work, "sdks/node/src/index.ts"), "export const a = 1;\nexport const b = 2;\n");
    writeFileSync(path.join(work, "sdks/node/CHANGELOG.md"), "# Changelog\n\n## [Unreleased]\n\n### Added\n- `b`.\n\n## [0.1.0] - 2026-10-30\n\n- First.\n");
    writeFileSync(path.join(work, "sdks/python/README.md"), "Docs.\n");
    g("add", "-A");
    g("commit", "--quiet", "-m", "Add b (#7)");
    g("push", "--quiet", "origin", "main", "--tags");
    const release = (...args) => execFileSync(process.execPath, ["scripts/release.mjs", "--skip-ci", "--no-ai", "--no-watch", ...args], { cwd: work, env: e, encoding: "utf8" });
    return { work, remote, g, release, e };
  }

  it("--dry-run reports the plan and changes nothing", () => {
    const { g, release } = fixture();
    const before = g("rev-parse", "HEAD");
    const out = release("--dry-run");
    assert.match(out, /node-v0\.1\.0 → node-v0\.2\.0/);
    assert.match(out, /Python \(PyPI\)\s+.*skip — only 1 non-shipping file/);
    assert.match(out, /## \[0\.2\.0\] - \d{4}-\d{2}-\d{2}\n\n### Added\n- `b`\./);
    assert.equal(g("rev-parse", "HEAD"), before);
    assert.equal(g("status", "--porcelain"), "");
    assert.equal(g("tag", "--list", "node-v0.2.0"), "");
  });

  it("commits, tags and pushes only the changed SDK", () => {
    const { work, remote, g, release, e } = fixture();
    const out = release("--yes");
    assert.match(out, /pushed main and node-v0\.2\.0/);
    assert.equal(JSON.parse(readFileSync(path.join(work, "sdks/node/package.json"), "utf8")).version, "0.2.0");
    const changelog = readFileSync(path.join(work, "sdks/node/CHANGELOG.md"), "utf8");
    assert.match(changelog, /## \[Unreleased\]\n\n## \[0\.2\.0\] - \d{4}-\d{2}-\d{2}\n\n### Added\n- `b`\.\n\n## \[0\.1\.0\]/);
    assert.equal(g("log", "-1", "--format=%s"), "Release node 0.2.0");
    const remoteRefs = execFileSync("git", ["ls-remote", "--tags", "--heads", remote], { env: e, encoding: "utf8" });
    assert.match(remoteRefs, /refs\/tags\/node-v0\.2\.0/);
    assert.doesNotMatch(remoteRefs, /python-v0\.2\.0/);
    assert.equal(g("rev-parse", "HEAD"), g("rev-parse", "origin/main"));
  });

  it("refuses to release from a dirty tree", () => {
    const { work, release } = fixture();
    writeFileSync(path.join(work, "sdks/node/src/index.ts"), "export const c = 3;\n");
    assert.throws(() => release("--yes"), /uncommitted changes/);
  });
});
