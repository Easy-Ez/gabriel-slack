"""Port of sdks/node/test/markdown.test.ts (plus Python-specific edge cases)."""

from __future__ import annotations

import re

import pytest

from gabriel.markdown.blocks_to_markdown import EntityResolver, convert_blocks_to_markdown, mrkdwn_to_markdown
from gabriel.markdown.chunker import (
    chunk_markdown,
    heal_open_fence,
    is_pictographic,
    measure_markdown,
    open_fence_at,
)

_USERS = {"U0ALICE0001": "Alice"}
_CHANNELS = {"C0GENERAL01": "general"}
_GROUPS = {"S0ONCALL001": "oncall"}

resolver = EntityResolver(user_name=_USERS.get, channel_name=_CHANNELS.get, usergroup_handle=_GROUPS.get)


# --------------------------------------------------------------------------- convert_blocks_to_markdown


def test_renders_rich_text_sections_with_styles_mentions_and_links() -> None:
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "user", "user_id": "U0ALICE0001"},
                        {"type": "text", "text": " please "},
                        {"type": "text", "text": "review", "style": {"bold": True}},
                        {"type": "text", "text": " in "},
                        {"type": "channel", "channel_id": "C0GENERAL01"},
                        {"type": "text", "text": " "},
                        {"type": "link", "url": "https://example.com", "text": "PR"},
                        {"type": "text", "text": " "},
                        {"type": "usergroup", "usergroup_id": "S0ONCALL001"},
                        {"type": "text", "text": " "},
                        {"type": "user", "user_id": "U0UNKNOWN01"},
                    ],
                },
                {
                    "type": "rich_text_preformatted",
                    "language": "ts",
                    "elements": [{"type": "text", "text": "const a = 1;\n"}],
                },
                {
                    "type": "rich_text_list",
                    "style": "ordered",
                    "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "one"}]}],
                },
                {"type": "rich_text_quote", "elements": [{"type": "text", "text": "quoted\nlines"}]},
            ],
        }
    ]
    assert convert_blocks_to_markdown(blocks, resolver) == (
        "@Alice please **review** in #general [PR](https://example.com) @oncall <@U0UNKNOWN01>"
        "\n\n```ts\nconst a = 1;\n```\n\n1. one\n\n> quoted\n> lines"
    )


def test_keeps_whitespace_outside_emphasis_and_uses_longer_code_spans_for_backticks() -> None:
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_section",
                    "elements": [
                        {"type": "text", "text": " bold ", "style": {"bold": True}},
                        {"type": "text", "text": "a`b", "style": {"code": True}},
                    ],
                }
            ],
        },
    ]
    assert convert_blocks_to_markdown(blocks) == " **bold** ``a`b``"


def test_renders_header_section_mrkdwn_and_divider() -> None:
    md = convert_blocks_to_markdown(
        [
            {"type": "header", "text": {"type": "plain_text", "text": "Deploy"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Ping <@U0ALICE0001> &amp; <https://x.dev|docs>"}},
            {"type": "divider"},
        ],
        resolver,
    )
    assert md == "# Deploy\n\nPing @Alice & [docs](https://x.dev)\n\n---"


def test_renders_lists_tables_emoji_and_combined_styles() -> None:
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_list",
                    "style": "check",
                    "elements": [
                        {"type": "rich_text_section", "checked": True, "elements": [{"type": "text", "text": "done"}]},
                        {"type": "rich_text_section", "elements": [{"type": "text", "text": "todo\nmore"}]},
                    ],
                },
                {
                    "type": "rich_text_list",
                    "style": "bullet",
                    "indent": 1,
                    "elements": [
                        {
                            "type": "rich_text_section",
                            "elements": [
                                {"type": "emoji", "name": "smile", "unicode": "1f604"},
                                {"type": "emoji", "name": "custom"},
                                {"type": "text", "text": "x", "style": {"bold": True, "italic": True, "strike": True}},
                            ],
                        }
                    ],
                },
            ],
        },
        {"type": "table", "rows": [[{"type": "raw_text", "text": "a|b"}, {"type": "raw_text", "text": "c"}], [{}]]},
    ]
    assert convert_blocks_to_markdown(blocks) == (
        "- [x] done\n- [ ] todo\n      more\n  - 😄:custom:***~~x~~***\n\n| a\\|b | c |\n| --- | --- |\n|  |"
    )


def test_ordered_list_offset_and_quote_border() -> None:
    blocks = [
        {
            "type": "rich_text",
            "elements": [
                {
                    "type": "rich_text_list",
                    "style": "ordered",
                    "offset": 2,
                    "border": 1,
                    "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": "three"}]}],
                },
            ],
        }
    ]
    assert convert_blocks_to_markdown(blocks) == "> 3. three"


def test_tolerates_malformed_blocks() -> None:
    assert convert_blocks_to_markdown(None) == ""
    assert convert_blocks_to_markdown("nope") == ""
    assert convert_blocks_to_markdown([1, None, "x", [], {"type": "rich_text", "elements": "bad"}]) == ""
    assert (
        convert_blocks_to_markdown([{"type": "header", "text": {"type": "plain_text", "text": "H"}, "level": 0}])
        == "# H"
    )
    # Invalid unicode hex falls back to the shortcode, like String.fromCodePoint throwing.
    bad = [{"type": "rich_text", "elements": [{"type": "rich_text_section", "elements": [
        {"type": "emoji", "name": "x", "unicode": "110000"},
        {"type": "emoji", "name": "y", "unicode": "1f604-"},
    ]}]}]  # fmt: skip
    assert convert_blocks_to_markdown(bad) == ":x::y:"


# --------------------------------------------------------------------------- mrkdwn_to_markdown


def test_mrkdwn_converts_entities() -> None:
    assert (
        mrkdwn_to_markdown(
            "<!here> <!subteam^S0ONCALL001|@oncall> <#C0GENERAL01|general> <@U0ALICE0001> <http://a.b|x>", resolver
        )
        == "@here @oncall #general @Alice [x](http://a.b)"
    )


def test_mrkdwn_unresolved_and_labels() -> None:
    assert mrkdwn_to_markdown("<@U0BOB00001> <@U0BOB00001|bob> <#C0RANDOM001> <!date^1|fallback> <!foo>") == (
        "<@U0BOB00001> @bob <#C0RANDOM001> fallback <!foo>"
    )
    assert mrkdwn_to_markdown("<https://a.b/(x)|label> &lt;b&gt; &amp;amp;") == "[label](https://a.b/(x%29) <b> &amp;"


# --------------------------------------------------------------------------- measure_markdown


def test_counts_code_points_and_weights_emoji_conservatively() -> None:
    assert measure_markdown("中文").code_points == 2
    m = measure_markdown("a😀")
    assert m.code_points == 2
    assert m.emoji_count == 1
    assert m.effective == 1 + 20


def test_newlines_weigh_two_and_pictographic_table() -> None:
    assert measure_markdown("a\nb").effective == 4
    assert is_pictographic("😀") and is_pictographic("©") and is_pictographic("❤")
    assert not is_pictographic("a") and not is_pictographic("中") and not is_pictographic("\U0001f3fb")


# --------------------------------------------------------------------------- chunk_markdown


def test_returns_the_text_unchanged_when_under_budget() -> None:
    assert chunk_markdown("short") == ["short"]


def test_splits_on_paragraph_boundaries_and_respects_the_budget() -> None:
    para = ("word " * 400).strip()
    text = "\n\n".join([para] * 12)
    pieces = chunk_markdown(text, 5000, 3000)
    assert len(pieces) > 1
    for p in pieces:
        assert measure_markdown(p).effective <= 5000
    assert re.sub(r"\s+", " ", " ".join(pieces)) == re.sub(r"\s+", " ", text)


def test_heals_code_fences_cut_mid_block() -> None:
    code = "\n".join(f"console.log({i});" for i in range(800))
    text = f"intro\n\n```js\n{code}\n```\n\nouter"
    pieces = chunk_markdown(text, 4000, 2000)
    assert len(pieces) > 2
    for p in pieces:
        assert open_fence_at(p) is None
        assert measure_markdown(p).effective <= 4000
    assert pieces[1].startswith("```js\n")


def test_repeats_table_headers_on_continuation() -> None:
    rows = "\n".join(f"| row {i} | value {i} |" for i in range(400))
    text = f"| name | value |\n| --- | --- |\n{rows}"
    pieces = chunk_markdown(text, 3000, 1500)
    assert len(pieces) > 1
    for p in pieces[1:]:
        assert p.startswith("| name | value |\n| --- | --- |")


def test_handles_emoji_heavy_text_by_effective_length() -> None:
    text = "😀" * 1500
    pieces = chunk_markdown(text)
    assert len(pieces) > 2
    for p in pieces:
        assert measure_markdown(p).effective <= 11_500
    assert "".join(pieces) == text


def test_mid_line_split_keeps_quote_prefix() -> None:
    text = "> " + ("abc " * 400).strip()
    pieces = chunk_markdown(text, 1000, 500)
    assert len(pieces) > 1
    for p in pieces:
        assert p.startswith("> ")
        assert measure_markdown(p).effective <= 1000


def test_rejects_tiny_budget() -> None:
    with pytest.raises(ValueError, match="chunk budget too small"):
        chunk_markdown("x", 199)


# --------------------------------------------------------------------------- heal_open_fence


def test_closes_an_open_fence_without_touching_closed_text() -> None:
    assert heal_open_fence("a\n```py\nx = 1") == "a\n```py\nx = 1\n```"
    assert heal_open_fence("```\nx\n```") == "```\nx\n```"


def test_open_fence_ignores_inline_code_and_crlf_lines() -> None:
    assert open_fence_at("```inline``` text") is None
    fence = open_fence_at("~~~~ py\ncode")
    assert fence is not None and fence.marker == "~~~~" and fence.lang == "py"
    # JS `.` stops at \r, so a fence line ending in \r is not a fence line.
    assert open_fence_at("```js\r\ncode") is None
