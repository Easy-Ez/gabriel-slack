import { describe, expect, it } from "vitest";
import { convertBlocksToMarkdown, mrkdwnToMarkdown } from "../src/markdown/blocks-to-markdown.js";
import { chunkMarkdown, healOpenFence, measureMarkdown, openFenceAt } from "../src/markdown/chunker.js";

const resolver = {
  userName: (id: string) => (id === "U0ALICE0001" ? "Alice" : undefined),
  channelName: (id: string) => (id === "C0GENERAL01" ? "general" : undefined),
  usergroupHandle: (id: string) => (id === "S0ONCALL001" ? "oncall" : undefined),
};

describe("convertBlocksToMarkdown", () => {
  it("renders rich text sections with styles, mentions and links", () => {
    const blocks = [
      {
        type: "rich_text",
        elements: [
          {
            type: "rich_text_section",
            elements: [
              { type: "user", user_id: "U0ALICE0001" },
              { type: "text", text: " please " },
              { type: "text", text: "review", style: { bold: true } },
              { type: "text", text: " in " },
              { type: "channel", channel_id: "C0GENERAL01" },
              { type: "text", text: " " },
              { type: "link", url: "https://example.com", text: "PR" },
              { type: "text", text: " " },
              { type: "usergroup", usergroup_id: "S0ONCALL001" },
              { type: "text", text: " " },
              { type: "user", user_id: "U0UNKNOWN01" },
            ],
          },
          { type: "rich_text_preformatted", language: "ts", elements: [{ type: "text", text: "const a = 1;\n" }] },
          { type: "rich_text_list", style: "ordered", elements: [{ type: "rich_text_section", elements: [{ type: "text", text: "one" }] }] },
          { type: "rich_text_quote", elements: [{ type: "text", text: "quoted\nlines" }] },
        ],
      },
    ];
    expect(convertBlocksToMarkdown(blocks, resolver)).toBe(
      "@Alice please **review** in #general [PR](https://example.com) @oncall <@U0UNKNOWN01>\n\n```ts\nconst a = 1;\n```\n\n1. one\n\n> quoted\n> lines",
    );
  });

  it("keeps whitespace outside emphasis and uses longer code spans for backticks", () => {
    const blocks = [
      { type: "rich_text", elements: [{ type: "rich_text_section", elements: [{ type: "text", text: " bold ", style: { bold: true } }, { type: "text", text: "a`b", style: { code: true } }] }] },
    ];
    expect(convertBlocksToMarkdown(blocks)).toBe(" **bold** ``a`b``");
  });

  it("renders header, section mrkdwn and divider", () => {
    const md = convertBlocksToMarkdown([
      { type: "header", text: { type: "plain_text", text: "Deploy" } },
      { type: "section", text: { type: "mrkdwn", text: "Ping <@U0ALICE0001> &amp; <https://x.dev|docs>" } },
      { type: "divider" },
    ], resolver);
    expect(md).toBe("# Deploy\n\nPing @Alice & [docs](https://x.dev)\n\n---");
  });
});

describe("mrkdwnToMarkdown", () => {
  it("converts entities", () => {
    expect(mrkdwnToMarkdown("<!here> <!subteam^S0ONCALL001|@oncall> <#C0GENERAL01|general> <@U0ALICE0001> <http://a.b|x>", resolver)).toBe(
      "@here @oncall #general @Alice [x](http://a.b)",
    );
  });
});

describe("measureMarkdown", () => {
  it("counts code points and weights emoji conservatively", () => {
    expect(measureMarkdown("中文").codePoints).toBe(2);
    const m = measureMarkdown("a😀");
    expect(m.codePoints).toBe(2);
    expect(m.emojiCount).toBe(1);
    expect(m.effective).toBe(1 + 20);
  });
});

describe("chunkMarkdown", () => {
  it("returns the text unchanged when under budget", () => {
    expect(chunkMarkdown("short")).toEqual(["short"]);
  });

  it("splits on paragraph boundaries and respects the budget", () => {
    const para = "word ".repeat(400).trim();
    const text = Array.from({ length: 12 }, () => para).join("\n\n");
    const pieces = chunkMarkdown(text, 5000, 3000);
    expect(pieces.length).toBeGreaterThan(1);
    for (const p of pieces) expect(measureMarkdown(p).effective).toBeLessThanOrEqual(5000);
    expect(pieces.join(" ").replace(/\s+/g, " ")).toBe(text.replace(/\s+/g, " "));
  });

  it("heals code fences cut mid-block", () => {
    const code = Array.from({ length: 800 }, (_, i) => `console.log(${i});`).join("\n");
    const text = `intro\n\n\`\`\`js\n${code}\n\`\`\`\n\nouter`;
    const pieces = chunkMarkdown(text, 4000, 2000);
    expect(pieces.length).toBeGreaterThan(2);
    for (const p of pieces) {
      expect(openFenceAt(p)).toBeUndefined();
      expect(measureMarkdown(p).effective).toBeLessThanOrEqual(4000);
    }
    expect(pieces[1]!.startsWith("```js\n")).toBe(true);
  });

  it("repeats table headers on continuation", () => {
    const rows = Array.from({ length: 400 }, (_, i) => `| row ${i} | value ${i} |`).join("\n");
    const text = `| name | value |\n| --- | --- |\n${rows}`;
    const pieces = chunkMarkdown(text, 3000, 1500);
    expect(pieces.length).toBeGreaterThan(1);
    for (const p of pieces.slice(1)) expect(p.startsWith("| name | value |\n| --- | --- |")).toBe(true);
  });

  it("handles emoji-heavy text by effective length", () => {
    const text = "😀".repeat(1500);
    const pieces = chunkMarkdown(text);
    expect(pieces.length).toBeGreaterThan(2);
    for (const p of pieces) expect(measureMarkdown(p).effective).toBeLessThanOrEqual(11_500);
    expect(pieces.join("")).toBe(text);
  });
});

describe("healOpenFence", () => {
  it("closes an open fence without touching closed text", () => {
    expect(healOpenFence("a\n```py\nx = 1")).toBe("a\n```py\nx = 1\n```");
    expect(healOpenFence("```\nx\n```")).toBe("```\nx\n```");
  });
});
