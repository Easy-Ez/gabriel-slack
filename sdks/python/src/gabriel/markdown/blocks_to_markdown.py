"""Converts Slack Block Kit ``blocks`` and legacy mrkdwn to standard Markdown.

Converts Slack Block Kit ``blocks`` (rich_text and common layout blocks) and legacy mrkdwn
text into standard Markdown that LLMs consume well. Mentions resolve to readable names
through an optional resolver; unresolved mentions keep their deterministic ``<@U…>`` /
``<#C…>`` form.

The input is untrusted raw Slack JSON, so every accessor tolerates arbitrary shapes. Where
JavaScript coercion rules are observable in the output (truthiness, ``Number()``,
``String.repeat``), they are reproduced so both SDKs render identical Markdown.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = ["EntityResolver", "convert_blocks_to_markdown", "mrkdwn_to_markdown"]


@dataclass(slots=True, kw_only=True)
class EntityResolver:
    """Optional name lookups. Each callable returns ``None`` when the entity is unknown."""

    user_name: Callable[[str], str | None] | None = None
    channel_name: Callable[[str], str | None] | None = None
    usergroup_handle: Callable[[str], str | None] | None = None


_EMPTY_RESOLVER = EntityResolver()

Node = dict[str, Any]

# ECMAScript WhiteSpace + LineTerminator (what JS `\s`, `trim()` match). Python's `\s` differs
# (it adds U+001C..U+001F and U+0085 and lacks U+FEFF).
_JS_WS_CLASS = "\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff"
_STYLE_SPLIT = re.compile(f"^([{_JS_WS_CLASS}]*)([\\s\\S]*?)([{_JS_WS_CLASS}]*)\\Z")
_HEX_CODEPOINTS = re.compile(r"^[0-9a-fA-F-]+\Z")
_JS_NUMBER = re.compile(r"^[+-]?(?:Infinity|(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?)\Z")
_JS_WS_CHARS = "\t\n\v\f\r \u00a0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff" + "".join(
    chr(c) for c in range(0x2000, 0x200B)
)


def _js_truthy(v: Any) -> bool:
    """JavaScript truthiness (objects and arrays are truthy even when empty)."""
    if v is None or v is False:
        return False
    if isinstance(v, bool):
        return True
    if isinstance(v, int | float):
        return v != 0 and not (isinstance(v, float) and math.isnan(v))
    if isinstance(v, str):
        return len(v) > 0
    return True


def _js_number(v: Any) -> float:
    """``Number(v)`` for JSON-shaped values (``NaN`` when not numeric)."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, int | float):
        return float(v)
    if v is None:
        return 0.0
    if isinstance(v, str):
        s = v.strip(_JS_WS_CHARS)
        if s == "":
            return 0.0
        if len(s) > 2 and s[0] == "0" and s[1] in "xXoObB":
            base = {"x": 16, "o": 8, "b": 2}[s[1].lower()]
            try:
                return float(int(s[2:], base))
            except ValueError:
                return math.nan
        if _JS_NUMBER.match(s):
            return float(s.replace("Infinity", "inf"))
        return math.nan
    if isinstance(v, list):
        if len(v) == 0:
            return 0.0
        if len(v) == 1:
            return _js_number(v[0]) if not isinstance(v[0], list | dict) else math.nan
    return math.nan


def _num_or(v: Any, default: float) -> float:
    """``Number(v ?? default)``."""
    return _js_number(default if v is None else v)


def _repeat(s: str, count: float) -> str:
    """``String.prototype.repeat`` (NaN → 0; negative / infinite counts throw)."""
    if math.isnan(count):
        return ""
    if count < 0 or math.isinf(count):
        raise ValueError(f"Invalid count value: {_js_num_str(count)}")
    return s * int(count)


def _js_num_str(n: float) -> str:
    """``String(number)`` for the values that appear here."""
    if math.isnan(n):
        return "NaN"
    if math.isinf(n):
        return "Infinity" if n > 0 else "-Infinity"
    if n == int(n) and abs(n) < 1e21:
        return str(int(n))
    return repr(n)


def _as_nodes(v: Any) -> list[Node]:
    """Objects in an array. Nested arrays are objects too in JS; they expose no Block Kit keys."""
    if not isinstance(v, list):
        return []
    out: list[Node] = []
    for x in v:
        if isinstance(x, dict):
            out.append(x)
        elif isinstance(x, list):
            out.append({})
    return out


def _str(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _user_name(r: EntityResolver, user_id: str) -> str | None:
    return r.user_name(user_id) if r.user_name is not None else None


def _channel_name(r: EntityResolver, channel_id: str) -> str | None:
    return r.channel_name(channel_id) if r.channel_name is not None else None


def _usergroup_handle(r: EntityResolver, subteam_id: str) -> str | None:
    return r.usergroup_handle(subteam_id) if r.usergroup_handle is not None else None


def _longest_run(text: str, ch: str) -> int:
    best = 0
    cur = 0
    for c in text:
        cur = cur + 1 if c == ch else 0
        best = max(best, cur)
    return best


def _code_span(text: str) -> str:
    fence = "`" * (_longest_run(text, "`") + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _apply_style(text: str, style: Any) -> str:
    if not _js_truthy(style) or len(text) == 0:
        return text
    # A string `style` exposes the legacy String.prototype.bold / .strike methods in JS (truthy),
    # so Node renders it bold + strikethrough; mirror that for identical output.
    st: Node = style if isinstance(style, dict) else {"bold": True, "strike": True} if isinstance(style, str) else {}
    if _js_truthy(st.get("code")):
        return _code_span(text)
    # Keep surrounding whitespace outside the emphasis markers so Markdown parses it.
    m = _STYLE_SPLIT.match(text)
    lead, core, trail = (m.group(1), m.group(2), m.group(3)) if m else ("", "", "")
    if not core:
        return text
    out = core
    if _js_truthy(st.get("strike")):
        out = f"~~{out}~~"
    if _js_truthy(st.get("italic")):
        out = f"*{out}*"
    if _js_truthy(st.get("bold")):
        out = f"**{out}**"
    return f"{lead}{out}{trail}"


def _emoji_from_unicode(unicode: str) -> str | None:
    """``String.fromCodePoint(...hex parts)``; ``None`` where JS would throw."""
    try:
        return "".join(chr(int(h, 16)) for h in unicode.split("-"))
    except (ValueError, OverflowError):
        return None


def _inline(elements: Any, r: EntityResolver) -> str:
    out = ""
    for el in _as_nodes(elements):
        style = el.get("style")
        t = el.get("type")
        if t == "text":
            out += _apply_style(_str(el.get("text")), style)
        elif t == "user":
            uid = _str(el.get("user_id"))
            name = _user_name(r, uid)
            out += _apply_style(f"@{name}" if name else f"<@{uid}>", style)
        elif t == "usergroup":
            gid = _str(el.get("usergroup_id"))
            handle = _usergroup_handle(r, gid)
            out += _apply_style(f"@{handle}" if handle else f"<!subteam^{gid}>", style)
        elif t == "channel":
            cid = _str(el.get("channel_id"))
            cname = _channel_name(r, cid)
            out += _apply_style(f"#{cname}" if cname else f"<#{cid}>", style)
        elif t == "broadcast":
            out += _apply_style(f"@{_str(el.get('range')) or 'channel'}", style)
        elif t == "emoji":
            unicode = _str(el.get("unicode"))
            if unicode and _HEX_CODEPOINTS.match(unicode):
                rendered = _emoji_from_unicode(unicode)
                if rendered is not None:
                    out += rendered
                    continue
                # fall through to shortcode
            out += f":{_str(el.get('name'))}:"
        elif t == "link":
            url = _str(el.get("url")).replace(")", "%29")
            text = _str(el.get("text"))
            out += _apply_style(f"[{text}]({url})" if text and text != el.get("url") else url, style)
        elif t == "date":
            out += _str(el.get("fallback")) or _str(el.get("timestamp"))
        else:
            raw_text = el.get("text")
            out += _apply_style(raw_text, style) if isinstance(raw_text, str) else _str(el.get("fallback"))
    return out


def _quote_prefix(text: str, depth: float) -> str:
    if depth <= 0:
        return text
    prefix = _repeat("> ", depth)
    return "\n".join(f"{prefix}{line}" for line in text.split("\n"))


def _preformatted(node: Node) -> str:
    code = ""
    for el in _as_nodes(node.get("elements")):
        t = el.get("type")
        if t == "text":
            code += _str(el.get("text"))
        elif t == "link":
            code += _str(el.get("text")) or _str(el.get("url"))
        elif t == "user":
            code += f"<@{_str(el.get('user_id'))}>"
        elif t == "channel":
            code += f"<#{_str(el.get('channel_id'))}>"
        elif t == "emoji":
            code += f":{_str(el.get('name'))}:"
    code = re.sub(r"\n+\Z", "", code, count=1)
    fence = "`" * max(3, _longest_run(code, "`") + 1)
    return _quote_prefix(f"{fence}{_str(node.get('language'))}\n{code}\n{fence}", _num_or(node.get("border"), 0))


def _list(node: Node, r: EntityResolver) -> str:
    indent = _repeat("  ", _num_or(node.get("indent"), 0))
    offset = _num_or(node.get("offset"), 0)
    items: list[str] = []
    for idx, item in enumerate(_as_nodes(node.get("elements"))):
        content = _inline(item.get("elements"), r)
        prefix = "- "
        if node.get("style") == "ordered":
            prefix = f"{_js_num_str(idx + 1 + offset)}. "
        elif node.get("style") == "check":
            prefix = f"- [{'x' if _js_truthy(item.get('checked')) else ' '}] "
        first, *rest = content.split("\n")
        pad = indent + " " * len(prefix)
        items.append("\n".join([f"{indent}{prefix}{first}", *(f"{pad}{line}" for line in rest)]))
    return _quote_prefix("\n".join(items), _num_or(node.get("border"), 0))


def _text_object(v: Any, r: EntityResolver) -> str:
    if not _js_truthy(v) or not isinstance(v, dict | list):
        return ""
    t: Node = v if isinstance(v, dict) else {}
    return mrkdwn_to_markdown(_str(t.get("text")), r) if t.get("type") == "mrkdwn" else _str(t.get("text"))


def _table(node: Node, r: EntityResolver) -> str:
    raw_rows = node.get("rows")
    rows: list[Any] = raw_rows if isinstance(raw_rows, list) else []
    if len(rows) == 0:
        return ""

    def cell(c: Any) -> str:
        n: Node = c if isinstance(c, dict) else {}
        text = convert_blocks_to_markdown([n], r) if n.get("type") == "rich_text" else _str(n.get("text"))
        return text.replace("|", "\\|").replace("\n", " ")

    lines = [f"| {' | '.join(cell(c) for c in (row if isinstance(row, list) else []))} |" for row in rows]
    cols = len(rows[0]) if isinstance(rows[0], list) else 1
    lines.insert(1, f"| {' | '.join('---' for _ in range(cols))} |")
    return "\n".join(lines)


def convert_blocks_to_markdown(blocks: Any, r: EntityResolver | None = None) -> str:
    """Converts a ``blocks`` array into standard Markdown. Returns ``""`` when nothing renders."""
    res = r if r is not None else _EMPTY_RESOLVER
    chunks: list[tuple[str, str]] = []  # (text, kind)
    for block in _as_nodes(blocks):
        bt = block.get("type")
        if bt == "header":
            text = _text_object(block.get("text"), res)
            if text:
                level = _num_or(block.get("level"), 1)
                if math.isnan(level) or level == 0:
                    level = 1
                chunks.append((f"{_repeat('#', level)} {text}", "header"))
        elif bt == "divider":
            chunks.append(("---", "divider"))
        elif bt == "section":
            parts = [
                _text_object(block.get("text"), res),
                *(_text_object(f, res) for f in _as_nodes(block.get("fields"))),
            ]
            parts = [p for p in parts if p]
            if parts:
                chunks.append(("\n".join(parts), "section"))
        elif bt == "context":
            parts = [
                _str(e.get("alt_text")) if e.get("type") == "image" else _text_object(e, res)
                for e in _as_nodes(block.get("elements"))
            ]
            parts = [p for p in parts if p]
            if parts:
                chunks.append((" ".join(parts), "context"))
        elif bt == "markdown":
            text = _str(block.get("text"))
            if text:
                chunks.append((text, "markdown"))
        elif bt == "table":
            text = _table(block, res)
            if text:
                chunks.append((text, "table"))
        elif bt == "rich_text":
            for sub in _as_nodes(block.get("elements")):
                st = sub.get("type")
                rendered = ""
                if st == "rich_text_section":
                    rendered = _inline(sub.get("elements"), res)
                elif st == "rich_text_quote":
                    rendered = _quote_prefix(_inline(sub.get("elements"), res), _num_or(sub.get("border"), 0) + 1)
                elif st == "rich_text_preformatted":
                    rendered = _preformatted(sub)
                elif st == "rich_text_list":
                    rendered = _list(sub, res)
                rendered = re.sub(r"\A\n+|\n+\Z", "", rendered)
                if rendered:
                    chunks.append((rendered, st if isinstance(st, str) else ""))
    out: list[str] = []
    for i, (text, kind) in enumerate(chunks):
        if i == 0:
            out.append(text)
            continue
        tight = chunks[i - 1][1] == "rich_text_list" and kind == "rich_text_list"
        out.append(("\n" if tight else "\n\n") + text)
    return "".join(out)


_ENTITY = re.compile(r"<([@#!])([^>|]+)(?:\|([^>]*))?>|<((?:https?|mailto):[^>|]+)(?:\|([^>]*))?>")


def mrkdwn_to_markdown(text: str, r: EntityResolver | None = None) -> str:
    """Converts Slack mrkdwn entities to Markdown.

    Handles ``<@U…>``, ``<#C…|name>``, ``<!here>``, ``<!subteam^S…|@handle>``, ``<url|label>``
    and HTML escapes. Emphasis styling is kept as-is because rewriting ``*bold*`` safely
    requires a full mrkdwn parser.
    """
    res = r if r is not None else _EMPTY_RESOLVER

    def replace(m: re.Match[str]) -> str:
        sigil, eid, label, url, url_label = m.group(1), m.group(2), m.group(3), m.group(4), m.group(5)
        if url:
            return f"[{url_label}]({url.replace(')', '%29')})" if url_label and url_label != url else url
        if sigil == "@" and eid:
            resolved = _user_name(res, eid)
            name = resolved if resolved is not None else label
            return f"@{re.sub(r'^@', '', name, count=1)}" if name else f"<@{eid}>"
        if sigil == "#" and eid:
            resolved = _channel_name(res, eid)
            name = resolved if resolved is not None else label
            return f"#{name}" if name else f"<#{eid}>"
        if sigil == "!" and eid:
            if eid.startswith("subteam^"):
                sid = eid[len("subteam^") :]
                resolved = _usergroup_handle(res, sid)
                handle = (
                    resolved
                    if resolved is not None
                    else (re.sub(r"^@", "", label, count=1) if label is not None else None)
                )
                return f"@{handle}" if handle else f"<!subteam^{sid}>"
            if eid in ("here", "channel", "everyone"):
                return f"@{eid}"
            return label if label is not None else f"<!{eid}>"
        return m.group(0)

    replaced = _ENTITY.sub(replace, text)
    return replaced.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
