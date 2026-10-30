import { describe, expect, it } from "vitest";
import { BatchLoader } from "../src/client/loader.js";
import { KeyedExecutor } from "../src/client/executor.js";
import { LoopBreaker, SelfRegistry } from "../src/client/origin.js";
import { presenceFrameBytes, PRESENCE_FRAME_MAX_BYTES } from "../src/client/presence.js";
import { GabrielError, fromSlackError } from "../src/errors.js";
import { MemoryStateStore } from "../src/state/store.js";
import { deferred } from "../src/util/async.js";
import { redactSecret, scrubSecrets } from "../src/util/redact.js";
import { PrioritySemaphore } from "../src/util/semaphore.js";
import { compareTs, normalizeEmoji } from "../src/util/validate.js";
import { tick } from "./harness.js";

describe("validation helpers", () => {
  it("compares Slack timestamps numerically", () => {
    expect(compareTs("1800000000.000100", "1800000000.000099")).toBeGreaterThan(0);
    expect(compareTs("999999999.9", "1000000000.0")).toBeLessThan(0);
    expect(compareTs("1.5", "1.500000")).toBe(0);
  });

  it("normalizes emoji names and rejects garbage", () => {
    expect(normalizeEmoji(":eyes:")).toBe("eyes");
    expect(() => normalizeEmoji("<script>")).toThrow(GabrielError);
  });
});

describe("redaction", () => {
  it("never prints the secret body", () => {
    const token = "xoxc-test-123456-abcdefabcdef-abcdefabcdefabcd";
    expect(redactSecret(token)).toMatch(/^xoxc-test-\.\.\.\[REDACTED, len=46\]$/);
    expect(scrubSecrets(`wss://x/?token=${token}&frt=abc d=xoxd-zzz`)).not.toContain("abcdefabcdef");
    expect(scrubSecrets("frt=SECRET")).not.toContain("SECRET");
  });
});

describe("error mapping", () => {
  it("maps Slack errors to codes with retryability", () => {
    expect(fromSlackError("chat.postMessage", "channel_not_found").code).toBe("CHANNEL_NOT_FOUND");
    expect(fromSlackError("x", "invalid_auth").code).toBe("TOKEN_EXPIRED");
    expect(fromSlackError("x", "invalid_auth").retryable).toBe(true);
    const unknown = fromSlackError("x", "weird_error");
    expect(unknown.code).toBe("SLACK_API_ERROR");
    expect(unknown.rawSlackError).toBe("weird_error");
  });
});

describe("KeyedExecutor", () => {
  it("runs same-key jobs serially in order and other keys in parallel", async () => {
    const ex = new KeyedExecutor<number>({ globalConcurrency: 4, perKeyConcurrency: 1, maxQueue: 100 });
    const log: string[] = [];
    let concurrentA = 0;
    let maxConcurrentA = 0;
    for (let i = 0; i < 5; i++) {
      ex.submit("A", i, async () => {
        concurrentA++;
        maxConcurrentA = Math.max(maxConcurrentA, concurrentA);
        await tick(Math.random() * 5);
        log.push(`A${i}`);
        concurrentA--;
      });
    }
    ex.submit("B", 0, async () => {
      log.push("B0");
    });
    await ex.idle();
    expect(maxConcurrentA).toBe(1);
    expect(log.filter((l) => l.startsWith("A"))).toEqual(["A0", "A1", "A2", "A3", "A4"]);
    expect(log.indexOf("B0")).toBeLessThan(log.indexOf("A4"));
  });

  it("rejects submissions beyond maxQueue", () => {
    const ex = new KeyedExecutor<number>({ globalConcurrency: 1, perKeyConcurrency: 1, maxQueue: 2 });
    const block = deferred<void>();
    expect(ex.submit("k", 0, () => block.promise)).toBe(true);
    expect(ex.submit("k", 1, async () => {})).toBe(true);
    expect(ex.submit("k", 2, async () => {})).toBe(true);
    expect(ex.submit("k", 3, async () => {})).toBe(false);
    block.resolve();
  });

  it("lets a suspended job yield its slot and resume ahead of queued work", async () => {
    const ex = new KeyedExecutor<string>({ globalConcurrency: 1, perKeyConcurrency: 1, maxQueue: 100 });
    const order: string[] = [];
    const reply = deferred<void>();
    ex.submit("k", "waiter", async (h) => {
      order.push("waiter:start");
      h.suspend();
      await reply.promise;
      await h.resume();
      order.push("waiter:resumed");
    });
    ex.submit("k", "next", async () => {
      order.push("next");
      reply.resolve();
      await tick(5);
      order.push("next:end");
    });
    ex.submit("k", "later", async () => {
      order.push("later");
    });
    await ex.idle();
    expect(order).toEqual(["waiter:start", "next", "next:end", "waiter:resumed", "later"]);
  });

  it("does not leak permits when a job resumes twice concurrently", async () => {
    const ex = new KeyedExecutor<string>({ globalConcurrency: 2, perKeyConcurrency: 1, maxQueue: 100 });
    const gate = deferred<void>();
    ex.submit("C1", "waiter", async (h) => {
      h.suspend();
      await gate.promise;
      await Promise.all([h.resume(), h.resume()]);
    });
    ex.submit("C1", "blocker", async () => {
      gate.resolve();
      await tick(10);
    });
    let ranAfter = false;
    ex.submit("C1", "after", async () => {
      ranAfter = true;
    });
    await ex.idle();
    expect(ranAfter).toBe(true);
    let again = false;
    ex.submit("C1", "again", async () => {
      again = true;
    });
    await ex.idle();
    expect(again).toBe(true);
  });

  it("extracts pending jobs by predicate", () => {
    const ex = new KeyedExecutor<string>({ globalConcurrency: 1, perKeyConcurrency: 1, maxQueue: 100 });
    const block = deferred<void>();
    ex.submit("k", "running", () => block.promise);
    ex.submit("k", "a", async () => {});
    ex.submit("k", "b", async () => {});
    expect(ex.extractPending("k", (m) => m === "b")).toBe("b");
    expect(ex.queueDepth).toBe(1);
    block.resolve();
  });
});

describe("BatchLoader", () => {
  it("batches within the window, shares in-flight lookups and caches misses", async () => {
    const batches: string[][] = [];
    const loader = new BatchLoader<string>({
      windowMs: 5,
      batchFn: async (ids) => {
        batches.push(ids);
        return new Map(ids.filter((id) => id !== "missing").map((id) => [id, id.toUpperCase()]));
      },
    });
    const results = await Promise.all(["a", "b", "a", "missing"].map((id) => loader.load(id)));
    expect(results).toEqual(["A", "B", "A", null]);
    expect(batches).toEqual([["a", "b", "missing"]]);
    expect(await loader.load("missing")).toBeNull();
    expect(batches.length).toBe(1);
  });

  it("does not cache transient errors", async () => {
    let fail = true;
    const loader = new BatchLoader<string>({
      windowMs: 1,
      batchFn: async (ids) => {
        if (fail) throw new GabrielError("NETWORK_TIMEOUT", "boom");
        return new Map(ids.map((id) => [id, "ok"]));
      },
    });
    await expect(loader.load("x")).rejects.toMatchObject({ code: "NETWORK_TIMEOUT" });
    fail = false;
    await expect(loader.load("x")).resolves.toBe("ok");
  });

  it("merges 300 lookups into at most 3 requests", async () => {
    let requests = 0;
    const loader = new BatchLoader<string>({
      batchFn: async (ids) => {
        requests++;
        return new Map(ids.map((id) => [id, id]));
      },
    });
    await Promise.all(Array.from({ length: 300 }, (_, i) => loader.load(`U${i}`)));
    expect(requests).toBeLessThanOrEqual(3);
  });
});

describe("SelfRegistry", () => {
  it("classifies origins", () => {
    const reg = new SelfRegistry(() => "U0SELF00001");
    reg.registerClientMsgId("cmid-1");
    expect(reg.classify({ user: "U0SELF00001", clientMsgId: "cmid-1", ts: "1.1", contentHash: "h", isEdit: false })).toBe("this_client");
    expect(reg.classify({ user: "U0SELF00001", ts: "1.2", contentHash: "h", isEdit: false })).toBe("self_other_client");
    expect(reg.classify({ user: "U0OTHER0001", ts: "1.3", contentHash: "h", isEdit: false })).toBe("other_user");
    expect(reg.classify({ user: "U0OTHER0001", botId: "B1", ts: "1.4", contentHash: "h", isEdit: false })).toBe("bot");
    reg.registerSentTs("1.5");
    reg.registerWrittenHash("1.5", "sdk-hash");
    expect(reg.classify({ user: "U0SELF00001", ts: "1.5", contentHash: "sdk-hash", isEdit: true })).toBe("this_client");
    expect(reg.classify({ user: "U0SELF00001", ts: "1.5", contentHash: "manual-edit", isEdit: true })).toBe("self_other_client");
  });
});

describe("LoopBreaker", () => {
  it("opens after N writes per minute per thread", () => {
    let now = 0;
    const lb = new LoopBreaker(3, () => now);
    for (let i = 0; i < 3; i++) expect(lb.tryRecord("C1", "1.0").allowed).toBe(true);
    expect(lb.tryRecord("C1", "1.0")).toEqual({ allowed: false, count: 3 });
    expect(lb.tryRecord("C1", "2.0").allowed).toBe(true);
    now = 61_000;
    expect(lb.tryRecord("C1", "1.0").allowed).toBe(true);
  });
});

describe("PrioritySemaphore", () => {
  it("reserves a slot for interactive work", async () => {
    const sem = new PrioritySemaphore(2, 1);
    const r1 = await sem.acquire("normal");
    let normalGranted = false;
    void sem.acquire("normal").then(() => (normalGranted = true));
    const r2 = await sem.acquire("interactive");
    await tick();
    expect(normalGranted).toBe(false);
    r1();
    await tick();
    // Normal work may only use total - reserved slots, so it waits for the interactive slot too.
    expect(normalGranted).toBe(false);
    r2();
    await tick();
    expect(normalGranted).toBe(true);
  });
});

describe("presence frame size", () => {
  it("the cap is enforced by serialized bytes, not ID count", () => {
    const ids = (n: number) => Array.from({ length: n }, (_, i) => `U${String(i).padStart(10, "0")}`);
    expect(presenceFrameBytes(ids(1100))).toBeLessThanOrEqual(PRESENCE_FRAME_MAX_BYTES);
    expect(presenceFrameBytes(ids(1200))).toBeGreaterThan(PRESENCE_FRAME_MAX_BYTES);
  });
});

describe("MemoryStateStore", () => {
  it("max-merges timestamps monotonically", async () => {
    const s = new MemoryStateStore();
    expect(await s.maxMergeTs("k", "1800000002.000100")).toBe("1800000002.000100");
    expect(await s.maxMergeTs("k", "1800000001.000100")).toBe("1800000002.000100");
    expect(await s.setIfAbsent("lock", "a")).toBe(true);
    expect(await s.setIfAbsent("lock", "b")).toBe(false);
  });
});
