#!/usr/bin/env node
// Cross-checks the language-neutral spec against the reference implementation:
// every event type in the TypeScript event map has a JSON Schema definition (and
// vice versa), and every fixture targets a known event type.
import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const schema = JSON.parse(readFileSync(path.join(root, "spec/schema/events.schema.json"), "utf8"));
const schemaEvents = new Set(schema.oneOf.map((r) => r.$ref.replace("#/$defs/", "")));

const typesTs = readFileSync(path.join(root, "sdks/node/src/events/types.ts"), "utf8");
const mapBody = typesTs.slice(typesTs.indexOf("export interface GabrielEventMap"));
const tsEvents = new Set([...mapBody.slice(0, mapBody.indexOf("}")).matchAll(/"([a-z_.]+)":/g)].map((m) => m[1]));

const problems = [];
for (const e of tsEvents) if (!schemaEvents.has(e)) problems.push(`schema is missing event "${e}"`);
for (const e of schemaEvents) if (!tsEvents.has(e)) problems.push(`TypeScript event map is missing "${e}"`);

const fxDir = path.join(root, "spec/fixtures/normalize");
for (const f of readdirSync(fxDir).filter((n) => n.endsWith(".json"))) {
  const fx = JSON.parse(readFileSync(path.join(fxDir, f), "utf8"));
  for (const key of ["description", "context", "frame", "expected"]) if (!(key in fx)) problems.push(`${f}: missing "${key}"`);
  if (fx.expected && !schemaEvents.has(fx.expected.type)) problems.push(`${f}: unknown event type "${fx.expected.type}"`);
}

if (problems.length) {
  console.error(problems.map((p) => `✗ ${p}`).join("\n"));
  process.exit(1);
}
console.log(`✓ spec consistent: ${schemaEvents.size} events, fixtures OK`);
