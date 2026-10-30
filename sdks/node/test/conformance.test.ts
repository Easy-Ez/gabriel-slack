import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { normalizeFrame } from "../src/events/normalize.js";

/**
 * Language-neutral conformance fixtures shared by every SDK: raw frame → normalized
 * event (after rendering with the fixture's entity names). Go / Python SDKs must
 * pass the same files.
 */
const dir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../../spec/fixtures/normalize");

interface Fixture {
  description: string;
  context: {
    selfUserId: string;
    mySubteams: string[];
    entities: { users: Record<string, string>; channels: Record<string, string>; usergroups: Record<string, string> };
  };
  frame: Record<string, unknown>;
  expected: Record<string, unknown> | null;
}

describe("conformance: normalize", () => {
  const files = readdirSync(dir).filter((f) => f.endsWith(".json"));
  it("has fixtures", () => expect(files.length).toBeGreaterThan(10));
  for (const file of files) {
    const fx = JSON.parse(readFileSync(path.join(dir, file), "utf8")) as Fixture;
    it(`${file}: ${fx.description}`, () => {
      const draft = normalizeFrame(fx.frame, {
        selfUserId: fx.context.selfUserId,
        isMySubteam: (id) => fx.context.mySubteams.includes(id),
      });
      if (fx.expected === null) {
        expect(draft).toBeNull();
        return;
      }
      expect(draft).not.toBeNull();
      const e = fx.context.entities;
      draft!.render({ userName: (id) => e.users[id], channelName: (id) => e.channels[id], usergroupHandle: (id) => e.usergroups[id] });
      // Serializable payload only (drop raw frames, which the fixtures do not repeat).
      const payload = JSON.parse(JSON.stringify(draft!.event)) as Record<string, unknown>;
      expect(payload).toMatchObject(fx.expected);
    });
  }
});
