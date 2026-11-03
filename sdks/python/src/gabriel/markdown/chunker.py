"""Syntax-aware Markdown chunker (spec/behavior.md §9).

Slack validates ``markdown_text`` twice: raw Unicode code points (<= 12,000, ``msg_too_long``)
and the length after emoji are expanded to ``:shortcode:`` (``msg_blocks_too_long``). Without a
full shortcode table every pictographic emoji is conservatively weighted as 20 characters.

Indices are Python string indices (code points); the Node SDK uses UTF-16 indices internally,
but every cut is derived from the same code-point weights, so the produced pieces are identical.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass

__all__ = [
    "DEFAULT_CHUNK_BUDGET",
    "DEFAULT_CHUNK_MIN",
    "EMOJI_WEIGHT",
    "SERVER_MAX_CODE_POINTS",
    "FenceState",
    "LengthMeasure",
    "chunk_markdown",
    "heal_open_fence",
    "is_pictographic",
    "measure_markdown",
    "open_fence_at",
]

SERVER_MAX_CODE_POINTS = 12_000
DEFAULT_CHUNK_BUDGET = 11_500
DEFAULT_CHUNK_MIN = 8_000
EMOJI_WEIGHT = 20

# Unicode `Extended_Pictographic` (the JS `/\p{Extended_Pictographic}/u` class; Python's `re` has
# no property escapes). Inclusive ranges, generated from V8 / ICU (Unicode 17.0).
_PICTOGRAPHIC_RANGES: tuple[tuple[int, int], ...] = (
    (0xA9, 0xA9), (0xAE, 0xAE), (0x203C, 0x203C), (0x2049, 0x2049), (0x2122, 0x2122), (0x2139, 0x2139),
    (0x2194, 0x2199), (0x21A9, 0x21AA), (0x231A, 0x231B), (0x2328, 0x2328), (0x23CF, 0x23CF), (0x23E9, 0x23F3),
    (0x23F8, 0x23FA), (0x24C2, 0x24C2), (0x25AA, 0x25AB), (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE),
    (0x2600, 0x2604), (0x260E, 0x260E), (0x2611, 0x2611), (0x2614, 0x2615), (0x2618, 0x2618), (0x261D, 0x261D),
    (0x2620, 0x2620), (0x2622, 0x2623), (0x2626, 0x2626), (0x262A, 0x262A), (0x262E, 0x262F), (0x2638, 0x263A),
    (0x2640, 0x2640), (0x2642, 0x2642), (0x2648, 0x2653), (0x265F, 0x2660), (0x2663, 0x2663), (0x2665, 0x2666),
    (0x2668, 0x2668), (0x267B, 0x267B), (0x267E, 0x267F), (0x2692, 0x2697), (0x2699, 0x2699), (0x269B, 0x269C),
    (0x26A0, 0x26A1), (0x26A7, 0x26A7), (0x26AA, 0x26AB), (0x26B0, 0x26B1), (0x26BD, 0x26BE), (0x26C4, 0x26C5),
    (0x26C8, 0x26C8), (0x26CE, 0x26CF), (0x26D1, 0x26D1), (0x26D3, 0x26D4), (0x26E9, 0x26EA), (0x26F0, 0x26F5),
    (0x26F7, 0x26FA), (0x26FD, 0x26FD), (0x2702, 0x2702), (0x2705, 0x2705), (0x2708, 0x270D), (0x270F, 0x270F),
    (0x2712, 0x2712), (0x2714, 0x2714), (0x2716, 0x2716), (0x271D, 0x271D), (0x2721, 0x2721), (0x2728, 0x2728),
    (0x2733, 0x2734), (0x2744, 0x2744), (0x2747, 0x2747), (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755),
    (0x2757, 0x2757), (0x2763, 0x2764), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0), (0x27BF, 0x27BF),
    (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55), (0x3030, 0x3030),
    (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299), (0x1F004, 0x1F004), (0x1F02C, 0x1F02F),
    (0x1F094, 0x1F09F), (0x1F0AF, 0x1F0B0), (0x1F0C0, 0x1F0C0), (0x1F0CF, 0x1F0D0), (0x1F0F6, 0x1F0FF),
    (0x1F170, 0x1F171), (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E), (0x1F191, 0x1F19A), (0x1F1AE, 0x1F1E5),
    (0x1F201, 0x1F20F), (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A), (0x1F23C, 0x1F23F),
    (0x1F249, 0x1F25F), (0x1F266, 0x1F321), (0x1F324, 0x1F393), (0x1F396, 0x1F397), (0x1F399, 0x1F39B),
    (0x1F39E, 0x1F3F0), (0x1F3F3, 0x1F3F5), (0x1F3F7, 0x1F3FA), (0x1F400, 0x1F4FD), (0x1F4FF, 0x1F53D),
    (0x1F549, 0x1F54E), (0x1F550, 0x1F567), (0x1F56F, 0x1F570), (0x1F573, 0x1F57A), (0x1F587, 0x1F587),
    (0x1F58A, 0x1F58D), (0x1F590, 0x1F590), (0x1F595, 0x1F596), (0x1F5A4, 0x1F5A5), (0x1F5A8, 0x1F5A8),
    (0x1F5B1, 0x1F5B2), (0x1F5BC, 0x1F5BC), (0x1F5C2, 0x1F5C4), (0x1F5D1, 0x1F5D3), (0x1F5DC, 0x1F5DE),
    (0x1F5E1, 0x1F5E1), (0x1F5E3, 0x1F5E3), (0x1F5E8, 0x1F5E8), (0x1F5EF, 0x1F5EF), (0x1F5F3, 0x1F5F3),
    (0x1F5FA, 0x1F64F), (0x1F680, 0x1F6C5), (0x1F6CB, 0x1F6D2), (0x1F6D5, 0x1F6E5), (0x1F6E9, 0x1F6E9),
    (0x1F6EB, 0x1F6F0), (0x1F6F3, 0x1F6FF), (0x1F7DA, 0x1F7FF), (0x1F80C, 0x1F80F), (0x1F848, 0x1F84F),
    (0x1F85A, 0x1F85F), (0x1F888, 0x1F88F), (0x1F8AE, 0x1F8AF), (0x1F8BC, 0x1F8BF), (0x1F8C2, 0x1F8CF),
    (0x1F8D9, 0x1F8FF), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945), (0x1F947, 0x1F9FF), (0x1FA58, 0x1FA5F),
    (0x1FA6E, 0x1FAFF), (0x1FC00, 0x1FFFD),
)  # fmt: skip
_PICTOGRAPHIC_STARTS = tuple(start for start, _ in _PICTOGRAPHIC_RANGES)

# ECMAScript WhiteSpace + LineTerminator, i.e. what JS `\s` / `trim()` match.
_JS_WS = "\t\n\v\f\r    -     　﻿"
_JS_WS_CHARS = "\t\n\v\f\r       　﻿" + "".join(chr(c) for c in range(0x2000, 0x200B))


def is_pictographic(ch: str) -> bool:
    """Whether the single code point ``ch`` has the ``Extended_Pictographic`` property."""
    cp = ord(ch)
    i = bisect.bisect_right(_PICTOGRAPHIC_STARTS, cp) - 1
    return i >= 0 and cp <= _PICTOGRAPHIC_RANGES[i][1]


@dataclass(frozen=True, slots=True)
class LengthMeasure:
    code_points: int
    emoji_count: int
    #: ``max(raw_code_points, expanded_blocks_length)`` — the value compared against the budget.
    effective: int


def measure_markdown(text: str) -> LengthMeasure:
    """Measures ``text`` the way Slack validates ``markdown_text``.

    Multipart form encoding normalizes every line break to CRLF before the server counts code
    points, so a newline is weighted as 2.
    """
    code_points = 0
    emoji_count = 0
    newlines = 0
    for cp in text:
        code_points += 1
        if cp == "\n":
            newlines += 1
        elif is_pictographic(cp):
            emoji_count += 1
    return LengthMeasure(
        code_points=code_points,
        emoji_count=emoji_count,
        effective=code_points + newlines + emoji_count * (EMOJI_WEIGHT - 1),
    )


@dataclass(frozen=True, slots=True)
class FenceState:
    marker: str
    lang: str


# JS `.` excludes all line terminators (\n, \r, U+2028, U+2029); `$` without /m = end of input.
_FENCE_LINE = re.compile(f"^[{_JS_WS}]{{0,3}}(`{{3,}}|~{{3,}})([^\\n\\r\\u2028\\u2029]*)\\Z")
_TABLE_SEP = re.compile(f"^[{_JS_WS}]*\\|?[{_JS_WS}]*:?-{{3,}}")
_LAST_WORD = re.compile(f"[{_JS_WS}][^{_JS_WS}]*\\Z")
_LEADING_WS = re.compile(f"^[{_JS_WS}]+")
_LINE_PREFIX = re.compile(f"^([{_JS_WS}]*(?:>[{_JS_WS}]?)*)")


def _js_trim(s: str) -> str:
    return s.strip(_JS_WS_CHARS)


def _js_trim_start(s: str) -> str:
    return s.lstrip(_JS_WS_CHARS)


def _js_trim_end(s: str) -> str:
    return s.rstrip(_JS_WS_CHARS)


def _last_index_of(text: str, needle: str, position: int) -> int:
    """``String.prototype.lastIndexOf(needle, position)`` (position clamped to ``[0, len]``)."""
    start = max(0, min(position, len(text)))
    return text.rfind(needle, 0, start + len(needle))


def open_fence_at(text: str) -> FenceState | None:
    """Returns the code fence still open at the end of ``text``, if any."""
    open_: FenceState | None = None
    for line in text.split("\n"):
        m = _FENCE_LINE.match(line)
        if not m:
            continue
        marker = m.group(1)
        rest = m.group(2) or ""
        if open_ is None:
            if marker.startswith("`") and "`" in rest:
                continue  # inline code, not a fence
            open_ = FenceState(marker=marker, lang=_js_trim(rest))
        elif marker[0] == open_.marker[0] and len(marker) >= len(open_.marker) and _js_trim(rest) == "":
            open_ = None
    return open_


def heal_open_fence(text: str) -> str:
    """Appends a closing fence when ``text`` ends inside a code block (render-time copy only)."""
    open_ = open_fence_at(text)
    if open_ is None:
        return text
    return f"{text}{'' if text.endswith(chr(10)) else chr(10)}{open_.marker}"


def _index_at_weight(text: str, weight: int) -> int:
    """Cumulative effective weight → string index."""
    acc = 0
    for idx, cp in enumerate(text):
        w = 2 if cp == "\n" else EMOJI_WEIGHT if is_pictographic(cp) else 1
        if acc + w > weight:
            return idx
        acc += w
    return len(text)


def _fence_ranges(text: str) -> list[tuple[int, int]]:
    """``[start, end)`` ranges covered by fenced code blocks (an unclosed fence runs to the end)."""
    ranges: list[tuple[int, int]] = []
    open_marker: str | None = None
    open_start = 0
    pos = 0
    for line in text.split("\n"):
        m = _FENCE_LINE.match(line)
        if m:
            marker = m.group(1)
            rest = m.group(2) or ""
            if open_marker is None:
                if not (marker.startswith("`") and "`" in rest):
                    open_marker, open_start = marker, pos
            elif marker[0] == open_marker[0] and len(marker) >= len(open_marker) and _js_trim(rest) == "":
                ranges.append((open_start, pos + len(line)))
                open_marker = None
        pos += len(line) + 1
    if open_marker is not None:
        ranges.append((open_start, len(text)))
    return ranges


def _last_index_outside_fence(text: str, ranges: list[tuple[int, int]], needle: str, from_: int, to: int) -> int:
    idx = _last_index_of(text, needle, to)
    while idx >= from_:
        if not any(s < idx < e for s, e in ranges):
            return idx
        idx = _last_index_of(text, needle, idx - 1)
    return -1


def _line_start(text: str, idx: int) -> int:
    return _last_index_of(text, "\n", idx - 1) + 1


def _table_header_before(text: str, idx: int) -> str | None:
    """If ``idx`` (a line start) sits inside a pipe table, returns the table's header + separator rows."""
    next_line_end = text.find("\n", idx)
    next_line = text[idx:] if next_line_end == -1 else text[idx:next_line_end]
    if not _js_trim_start(next_line).startswith("|"):
        return None
    before = text[:idx]
    if before.endswith("\n"):
        before = before[:-1]
    lines = before.split("\n")
    start = len(lines) - 1
    while start >= 0 and _js_trim_start(lines[start]).startswith("|"):
        start -= 1
    start += 1
    if start + 1 >= len(lines):
        return None
    header = lines[start]
    sep = lines[start + 1]
    if not _TABLE_SEP.match(sep):
        return None
    if start + 1 >= len(lines) - 1 and len(lines) - start <= 2:
        return None  # cut right after the header
    return f"{header}\n{sep}"


@dataclass(frozen=True, slots=True)
class _Cut:
    index: int
    skip: int
    mid_line: bool


def _find_cut(text: str, budget: int, min_chunk: int, reserve: int) -> _Cut:
    max_idx = max(1, _index_at_weight(text, budget - reserve))
    min_idx = min(max_idx, _index_at_weight(text, min(min_chunk, budget - reserve)))
    ranges = _fence_ranges(text[: max_idx + 2])
    i = _last_index_outside_fence(text, ranges, "\n\n", min_idx, max_idx)
    if i > 0:
        return _Cut(index=i, skip=2, mid_line=False)
    i = _last_index_outside_fence(text, ranges, "\n", min_idx, max_idx)
    if i > 0:
        return _Cut(index=i, skip=1, mid_line=False)
    i = _last_index_of(text, "\n", max_idx)
    if i >= min_idx and i > 0:
        return _Cut(index=i, skip=1, mid_line=False)
    window = text[min_idx:max_idx]
    ws = _LAST_WORD.search(window)
    if ws is not None:
        return _Cut(index=min_idx + ws.start(), skip=1, mid_line=True)
    return _Cut(index=max_idx, skip=0, mid_line=True)


def chunk_markdown(text: str, budget: int = DEFAULT_CHUNK_BUDGET, min_chunk: int = DEFAULT_CHUNK_MIN) -> list[str]:
    """Splits Markdown into pieces whose effective length fits ``budget``.

    Prefers blank lines → newlines → word boundaries outside code blocks. Code fences cut
    mid-block are closed and reopened (same language); tables repeat their header; a line
    split mid-way keeps its quote / indent prefix.

    Raises ``ValueError`` when ``budget < 200`` (Node: ``RangeError``).
    """
    if budget < 200:
        raise ValueError("chunk budget too small")
    chunks: list[str] = []
    rest = text
    guard = 0
    while measure_markdown(rest).effective > budget:
        guard += 1
        if guard > 10_000:
            raise RuntimeError("chunker failed to make progress")
        cut = _find_cut(rest, budget, min_chunk, 64)
        piece = rest[: cut.index]
        nxt = rest[cut.index + cut.skip :]
        fence = open_fence_at(piece)
        if fence is not None:
            piece = f"{piece.removesuffix(chr(10))}\n{fence.marker}"
            nxt = f"{fence.marker}{fence.lang}\n{nxt}"
        elif cut.mid_line:
            line = rest[_line_start(rest, cut.index) : cut.index]
            m = _LINE_PREFIX.match(line)
            prefix = m.group(1) if m else ""
            nxt = f"{prefix}{_LEADING_WS.sub('', nxt, count=1)}"
        else:
            header = _table_header_before(rest, cut.index + cut.skip)
            if header:
                nxt = f"{header}\n{nxt}"
        piece = _js_trim_end(piece)
        if len(piece) > 0:
            chunks.append(piece)
        rest = nxt
    if len(_js_trim(rest)) > 0 or len(chunks) == 0:
        chunks.append(rest)
    return chunks
