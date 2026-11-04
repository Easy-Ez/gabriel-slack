import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { normalizeFrame } from "../src/events/normalize.js";
import type { MessageCreatedEvent } from "../src/events/types.js";

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

describe("mention reasons", () => {
  const frame = { type: "message", channel: "C0GENERAL01", user: "U0ALICE0001", ts: "1800000001.000100" };
  const created = (text: string, isMySubteam?: (id: string) => boolean | undefined) =>
    normalizeFrame({ ...frame, text }, { selfUserId: "U0SELF00001", ...(isMySubteam ? { isMySubteam } : {}) })!.event as MessageCreatedEvent;

  it("lists each reason once and keeps isMentioningMe in step", () => {
    const cases: Array<[string, string[]]> = [
      ["hello", []],
      ["<@U0ALICE0001> hi", []],
      ["<@U0SELF00001> hi", ["direct"]],
      ["<!subteam^S0ONCALL001> hi", ["usergroup"]],
      ["<!subteam^S0DESIGN001> hi", []],
      ["<!here> <!everyone> hi", ["broadcast"]],
      ["<!channel> <!subteam^S0ONCALL001> <@U0SELF00001>", ["direct", "usergroup", "broadcast"]],
    ];
    for (const [text, reasons] of cases) {
      const evt = created(text, (id) => id === "S0ONCALL001");
      expect(evt.mentionReasons, text).toEqual(reasons);
      expect(evt.isMentioningMe, text).toBe(reasons.length > 0);
    }
  });

  it("sets includesMe only on usergroup mentions with a known membership", () => {
    const known = created("<!subteam^S0ONCALL001> <!subteam^S0DESIGN001> <@U0SELF00001> <!here>", (id) => id === "S0ONCALL001");
    expect(known.mentions).toEqual([
      { kind: "usergroup", id: "S0ONCALL001", includesMe: true },
      { kind: "usergroup", id: "S0DESIGN001", includesMe: false },
      { kind: "user", id: "U0SELF00001" },
      { kind: "broadcast", id: "here" },
    ]);
    const unknown = created("<!subteam^S0ONCALL001> hi", (id) => (id === "S0DESIGN001" ? false : undefined));
    expect(unknown.mentions).toEqual([{ kind: "usergroup", id: "S0ONCALL001" }]);
    expect(unknown.mentionReasons).toEqual([]);
    const pure = created("<!subteam^S0ONCALL001> hi");
    expect(pure.mentions).toEqual([{ kind: "usergroup", id: "S0ONCALL001" }]);
    expect(pure.isMentioningMe).toBe(false);
  });
});
