#!/usr/bin/env node
// `pnpm release`: works out which SDKs need a release, computes their next versions from the
// public API diff, drafts the CHANGELOG entries, and after one confirmation commits, tags and
// pushes. The tag-triggered workflows (.github/workflows/release-*.yml) do the publishing;
// this script then follows them until the versions are live. Run with --help for options.
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { createInterface } from "node:readline/promises";
import { fileURLToPath } from "node:url";
import {
  PACKAGES,
  PACKAGE_NAME,
  addRelease,
  apiDiffSince,
  bumpVersion,
  changedFiles,
  classifyFiles,
  commitsSince,
  compareVersions,
  decideBump,
  draftPrompt,
  fallbackNotes,
  hasCommand,
  lastReleaseNotes,
  latestTag,
  parseArgs,
  readVersion,
  run,
  tryRun,
  unreleasedNotes,
  withNodeVersion,
} from "./release/lib.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const HELP = `Usage: pnpm release [options]

Detects which SDKs changed since their last release tag, proposes versions and CHANGELOG
entries, and after one confirmation commits, tags (node-v*, python-v*) and pushes. The release
workflows publish; the script waits for them and checks npm / PyPI.

Options:
  --dry-run              Show the plan only; change nothing
  --only node,python     Release only these SDKs (also forces a release without source changes)
  --bump [pkg=]kind      Override the computed bump: patch | minor | major
  --version pkg=x.y.z    Release this exact version
  --no-ai                Do not draft notes with the claude CLI (use commit subjects)
  --skip-ci              Do not require a green CI run on HEAD
  --no-watch             Do not follow the release workflows after pushing
  -y, --yes              Do not ask for confirmation or open an editor
  -h, --help             Show this help`;

const c = {
  bold: (s) => `\x1b[1m${s}\x1b[0m`,
  dim: (s) => `\x1b[2m${s}\x1b[0m`,
  red: (s) => `\x1b[31m${s}\x1b[0m`,
  green: (s) => `\x1b[32m${s}\x1b[0m`,
  yellow: (s) => `\x1b[33m${s}\x1b[0m`,
};
const say = (s = "") => process.stdout.write(`${s}\n`);
const warn = (s) => say(c.yellow(`! ${s}`));
const fail = (s) => {
  process.stderr.write(`${c.red(`✗ ${s}`)}\n`);
  process.exit(1);
};
const git = (...args) => run("git", args, { cwd: root });

async function ask(question) {
  const rl = createInterface({ input: process.stdin, output: process.stdout });
  try {
    return (await rl.question(question)).trim();
  } finally {
    rl.close();
  }
}

function checkRepository(opts) {
  const problems = [];
  const branch = git("rev-parse", "--abbrev-ref", "HEAD");
  if (branch !== "main") problems.push(`on branch "${branch}", releases are cut from main`);
  if (git("status", "--porcelain")) problems.push("working tree has uncommitted changes (they are not part of a release)");
  const fetched = tryRun("git", ["fetch", "--quiet", "--tags", "origin", "main"], { cwd: root });
  if (!fetched.ok) problems.push(`git fetch failed: ${fetched.err.trim()}`);
  else if (branch === "main" && git("rev-parse", "HEAD") !== git("rev-parse", "origin/main")) problems.push("main is not in sync with origin/main (pull or push first)");
  if (problems.length && !opts.dryRun) fail(`cannot release:\n  - ${problems.join("\n  - ")}`);
  for (const p of problems) warn(`${p} — ignored for --dry-run`);
}

function checkCi(opts) {
  if (opts.skipCi) return;
  if (!hasCommand("gh")) return warn("gh is not installed; CI status not checked");
  const sha = git("rev-parse", "HEAD");
  const res = tryRun("gh", ["run", "list", "--commit", sha, "--workflow", "ci.yml", "--json", "status,conclusion,url", "--limit", "5"], { cwd: root });
  if (!res.ok) return warn(`could not read CI status: ${res.err.trim().split("\n")[0]}`);
  const runs = JSON.parse(res.out || "[]");
  const latest = runs[0];
  let problem;
  if (!latest) problem = `no CI run for ${sha.slice(0, 7)} yet`;
  else if (latest.status !== "completed") problem = `CI is still running: ${latest.url}`;
  else if (latest.conclusion !== "success") problem = `CI ${latest.conclusion}: ${latest.url}`;
  if (!problem) return say(c.green(`✓ CI passed on ${sha.slice(0, 7)}`));
  if (opts.dryRun) return warn(`${problem} — ignored for --dry-run`);
  fail(`${problem} (use --skip-ci to release anyway)`);
}

async function plan(pkg, opts, specChanged) {
  const tag = latestTag(root, pkg.tagPrefix);
  const current = readVersion(root, pkg);
  const files = changedFiles(root, tag, [pkg.dir]);
  const { shipping, other } = classifyFiles(files, pkg);
  const forced = opts.only?.includes(pkg.id) ?? false;
  const p = { pkg, tag, current, shipping, other, forced };
  if (!shipping.length && !forced) return { ...p, release: false };
  if (specChanged && !shipping.length) warn(`${pkg.id}: spec/ changed since ${tag} but ${pkg.dir}/src did not`);

  const tagVersion = tag ? tag.slice(pkg.tagPrefix.length) : null;
  const apiDiff = await apiDiffSince(root, pkg, tag);
  const diff = apiDiff && !apiDiff.error ? apiDiff : null;
  if (apiDiff?.error) warn(`${pkg.id}: public API not compared (${apiDiff.error})`);
  let decision = decideBump(tagVersion ?? current, diff);
  let next;
  if (opts.version[pkg.id]) {
    next = opts.version[pkg.id];
    decision = { kind: "manual", reason: "--version" };
  } else if (opts.bump[pkg.id] ?? opts.bump["*"]) {
    const kind = opts.bump[pkg.id] ?? opts.bump["*"];
    next = bumpVersion(tagVersion ?? current, kind);
    decision = { kind, reason: "--bump" };
  } else if (tagVersion && compareVersions(current, tagVersion) > 0) {
    next = current;
    decision = { kind: "manual", reason: `${pkg.versionFile} already says ${current}` };
  } else {
    next = bumpVersion(tagVersion ?? current, decision.kind);
  }
  if (tagVersion && compareVersions(next, tagVersion) <= 0) fail(`${pkg.id}: ${next} is not newer than ${tag}`);
  const commits = commitsSince(root, tag, [pkg.dir, "spec"]);
  return { ...p, release: true, next, decision, diff, commits };
}

function prDetails(commits) {
  if (!hasCommand("gh")) return [];
  const numbers = [...new Set(commits.map((x) => x.pr).filter(Boolean))];
  return numbers.flatMap((n) => {
    const res = tryRun("gh", ["pr", "view", String(n), "--json", "number,title,body"], { cwd: root });
    return res.ok ? [JSON.parse(res.out)] : [];
  });
}

function draftNotes(p, opts) {
  const changelog = readFileSync(path.join(root, p.pkg.changelog), "utf8");
  const existing = unreleasedNotes(changelog);
  if (existing) return { notes: existing, source: "the [Unreleased] section" };
  if (opts.ai && hasCommand("claude")) {
    say(c.dim(`  drafting ${p.pkg.id} notes with claude…`));
    const prompt = draftPrompt({
      pkg: p.pkg,
      version: p.next,
      commits: p.commits,
      prs: prDetails(p.commits),
      apiDiff: p.diff,
      styleExample: lastReleaseNotes(changelog),
      diffStat: p.tag ? tryRun("git", ["diff", "--stat", `${p.tag}..HEAD`, "--", p.pkg.dir], { cwd: root }).out : "",
    });
    const res = tryRun("claude", ["-p"], { cwd: root, input: prompt, timeout: 300_000 });
    const notes = res.ok ? res.out.replace(/^```(?:markdown)?\n|```$/g, "").trim() : "";
    if (/^### /m.test(notes)) return { notes, source: "claude (review it)" };
    warn(`claude did not return usable notes${res.ok ? "" : `: ${res.err.trim().split("\n")[0]}`}; using commit subjects`);
  }
  return { notes: fallbackNotes(p.commits) || "### Changed\n- Maintenance release.", source: "commit subjects" };
}

function edit(text, label) {
  const editor = (process.env.VISUAL || process.env.EDITOR || "").trim();
  if (!editor || !process.stdin.isTTY) return text;
  const dir = mkdtempSync(path.join(os.tmpdir(), "gabriel-release-"));
  const file = path.join(dir, `${label}-CHANGELOG.md`);
  try {
    writeFileSync(file, `${text}\n`);
    const [cmd, ...args] = editor.split(/\s+/);
    const res = spawnSync(cmd, [...args, file], { stdio: "inherit" });
    if (res.status !== 0) warn(`editor exited with ${res.status}; keeping the draft`);
    return readFileSync(file, "utf8").trim();
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}

function printPlan(plans) {
  say();
  say(c.bold("Release plan"));
  for (const p of plans) {
    if (!p.release) {
      const why = p.other.length ? `only ${p.other.length} non-shipping file(s) changed` : "no changes";
      say(`  ${p.pkg.label.padEnd(14)} ${c.dim(`skip — ${why} since ${p.tag ?? "the beginning"}`)}`);
      continue;
    }
    say(`  ${p.pkg.label.padEnd(14)} ${c.bold(`${p.tag ?? p.current} → ${p.pkg.tagPrefix}${p.next}`)}  ${c.dim(`${p.decision.kind}: ${p.decision.reason}`)}`);
    say(c.dim(`    ${p.shipping.length} shipping file(s), ${p.commits.length} commit(s)${p.forced && !p.shipping.length ? ", forced by --only" : ""}`));
    if (p.diff) {
      const show = (sign, keys) => keys.slice(0, 8).forEach((k) => say(c.dim(`    ${sign} ${k}`)));
      show("+", p.diff.added);
      show("-", p.diff.removed);
      show("~", p.diff.changed);
      const total = p.diff.added.length + p.diff.removed.length + p.diff.changed.length;
      if (total > 24) say(c.dim(`    … ${total} API changes in total`));
    }
  }
}

function apply(plans, date) {
  const files = [];
  for (const p of plans) {
    const versionPath = path.join(root, p.pkg.versionFile);
    if (p.pkg.id === "node") {
      writeFileSync(versionPath, withNodeVersion(readFileSync(versionPath, "utf8"), p.next));
    } else {
      run("uv", ["version", p.next], { cwd: path.join(root, p.pkg.dir) });
      if (git("status", "--porcelain", "--", `${p.pkg.dir}/uv.lock`)) files.push(`${p.pkg.dir}/uv.lock`);
    }
    const changelogPath = path.join(root, p.pkg.changelog);
    writeFileSync(changelogPath, addRelease(readFileSync(changelogPath, "utf8"), p.next, date, p.notes));
    files.push(p.pkg.versionFile, p.pkg.changelog);
  }
  const tags = plans.map((p) => `${p.pkg.tagPrefix}${p.next}`);
  const message = `Release ${plans.map((p) => `${p.pkg.id} ${p.next}`).join(", ")}`;
  git("add", "--", ...files);
  git("commit", "--quiet", "-m", message);
  for (const p of plans) git("tag", "-a", `${p.pkg.tagPrefix}${p.next}`, "-m", `${PACKAGE_NAME} ${p.next} (${p.pkg.label})`);
  say(c.green(`✓ committed "${message}" and tagged ${tags.join(", ")}`));
  const pushed = tryRun("git", ["push", "--atomic", "origin", "HEAD:main", ...tags], { cwd: root });
  if (!pushed.ok) {
    fail(
      `push failed: ${pushed.err.trim()}\nNothing was published. Retry with: git push --atomic origin HEAD:main ${tags.join(" ")}\n` +
        `or undo locally with: git tag -d ${tags.join(" ")} && git reset --hard HEAD~1`,
    );
  }
  say(c.green(`✓ pushed main and ${tags.join(", ")}`));
  return tags;
}

async function sleep(ms) {
  await new Promise((r) => setTimeout(r, ms));
}

async function follow(p) {
  const tag = `${p.pkg.tagPrefix}${p.next}`;
  let found;
  for (let i = 0; i < 30 && !found; i++) {
    const res = tryRun("gh", ["run", "list", "--workflow", p.pkg.workflow, "--json", "databaseId,headBranch,url", "--limit", "10"], { cwd: root });
    found = res.ok ? JSON.parse(res.out || "[]").find((r) => r.headBranch === tag) : undefined;
    if (!found) await sleep(4_000);
  }
  if (!found) return warn(`${tag}: release workflow run not found; check https://github.com/${repoSlug()}/actions`);
  say(c.bold(`\n${tag}: following ${found.url}`));
  const watched = spawnSync("gh", ["run", "watch", String(found.databaseId), "--exit-status", "--interval", "10"], { cwd: root, stdio: "inherit" });
  if (watched.status !== 0) return say(c.red(`✗ ${tag}: release workflow failed — ${found.url}`));
  await checkRegistry(p);
}

async function checkRegistry(p) {
  for (let i = 0; i < 20; i++) {
    let live = false;
    if (p.pkg.id === "node") live = tryRun("npm", ["view", `${PACKAGE_NAME}@${p.next}`, "version"]).out === p.next;
    else live = (await fetch(`https://pypi.org/pypi/${PACKAGE_NAME}/${p.next}/json`).catch(() => null))?.ok === true;
    if (live) return say(c.green(`✓ ${PACKAGE_NAME} ${p.next} is live on ${p.pkg.id === "node" ? "npm" : "PyPI"}`));
    await sleep(15_000);
  }
  if (p.pkg.id === "node") say(c.yellow(`! ${PACKAGE_NAME}@${p.next} published but not installable yet: npm shows new versions as "Validating" for a while. Nothing to re-run.`));
  else warn(`${PACKAGE_NAME} ${p.next} is not on PyPI yet; check https://pypi.org/project/${PACKAGE_NAME}/`);
}

function repoSlug() {
  const res = tryRun("gh", ["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"], { cwd: root });
  return res.ok ? res.out : "<owner>/<repo>";
}

async function main() {
  let opts;
  try {
    opts = parseArgs(process.argv.slice(2));
  } catch (err) {
    fail(err.message);
  }
  if (opts.help) return say(HELP);

  checkRepository(opts);
  checkCi(opts);

  const ids = opts.only ?? Object.keys(PACKAGES);
  const tags = ids.map((id) => latestTag(root, PACKAGES[id].tagPrefix));
  const oldest = tags.includes(null) ? null : tags.reduce((a, b) => (tryRun("git", ["merge-base", "--is-ancestor", a, b], { cwd: root }).ok ? a : b));
  const specChanged = changedFiles(root, oldest, ["spec"]).length > 0;
  const plans = [];
  for (const id of ids) plans.push(await plan(PACKAGES[id], opts, specChanged));
  printPlan(plans);
  const releasing = plans.filter((p) => p.release);
  if (!releasing.length) return say(c.green("\nNothing to release."));

  for (const p of releasing) {
    const { notes, source } = draftNotes(p, opts);
    p.notes = notes;
    p.notesSource = source;
  }
  if (!opts.dryRun && !opts.yes) {
    for (const p of releasing) {
      say(c.dim(`\nOpening the ${p.pkg.id} notes in your editor (from ${p.notesSource})…`));
      p.notes = edit(p.notes, p.pkg.id);
    }
  }
  const date = new Date().toISOString().slice(0, 10);
  for (const p of releasing) {
    say(c.bold(`\n${p.pkg.changelog}`) + c.dim(`  (from ${p.notesSource})`));
    say(`## [${p.next}] - ${date}\n\n${p.notes}`);
  }
  if (opts.dryRun) return say(c.dim("\n--dry-run: nothing was changed."));
  if (!opts.yes) {
    const tagsText = releasing.map((p) => `${p.pkg.tagPrefix}${p.next}`).join(", ");
    const answer = await ask(`\nCommit, tag ${tagsText} and push to origin/main? This publishes. [y/N] `);
    if (!/^y(es)?$/i.test(answer)) return say("Cancelled; nothing was changed.");
  }
  apply(releasing, date);
  if (!opts.watch) return say(c.dim("Not following the release workflows (--no-watch)."));
  if (!hasCommand("gh")) return warn("gh is not installed; follow the release workflows on GitHub Actions");
  for (const p of releasing) await follow(p);
}

await main();
