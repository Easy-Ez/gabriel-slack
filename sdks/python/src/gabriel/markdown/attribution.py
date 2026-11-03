"""Agent attribution line (spec/behavior.md §9.1).

A context block above the message body that says an agent wrote it. It is a self-declared
display hint — any client can write the same block — never proof of who wrote a message.

Attribution options are plain dicts (``TypedDict``) rather than dataclasses because their
semantics depend on which keys are present: a per-write override such as
``{"review": "confirmed"}`` is merged over the client default key by key, and ``"icon": None``
(omit the icon) differs from leaving ``icon`` out (use ``:robot_face:``).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal, Required, TypeAlias, TypedDict, TypeGuard, cast

from ..errors import GabrielError
from ..events.types import AttributionReview, MessageAttribution
from .blocks_to_markdown import EntityResolver, convert_blocks_to_markdown
from .chunker import _JS_WS, EMOJI_WEIGHT, _js_trim, measure_markdown

__all__ = [
    "ATTRIBUTION_BLOCK_ID",
    "DEFAULT_ATTRIBUTION_ICON",
    "Attribution",
    "AttributionCustom",
    "AttributionInfo",
    "AttributionOverride",
    "AttributionPatch",
    "AttributionPreset",
    "AttributionReview",
    "AttributionScope",
    "DefaultAttribution",
    "MessageAttribution",
    "RenderedAttribution",
    "attribution_user_ids",
    "markdown_to_plain_text",
    "parse_attribution",
    "render_attribution",
    "resolve_attribution",
    "split_attribution",
    "validate_attribution",
]


class AttributionPreset(TypedDict, total=False):
    """Preset line.

    ``{"icon": ":robot_face:", "name": "Paimon", "role": "Assistant", "review": "confirmed"}``
    renders ``:robot_face: Paimon (Assistant) · confirmed by <@SELF>``.
    """

    #: Leading emoji shortcode. Default ``":robot_face:"``; ``None`` omits it.
    icon: str | None
    #: Agent name. Required, single line, at most 64 characters.
    name: Required[str]
    #: Shown in parentheses after the name. Optional, at most 64 characters.
    role: str
    #: Default ``"unconfirmed"``.
    review: AttributionReview


class AttributionPatch(TypedDict, total=False):
    """Partial preset for a per-write override, merged key by key over the client default."""

    icon: str | None
    name: str
    role: str
    review: AttributionReview


@dataclass(kw_only=True, slots=True, frozen=True)
class AttributionInfo:
    """Argument of a custom ``text`` callable."""

    self_user_id: str
    channel: str
    review: AttributionReview
    thread_ts: str | None = None


class AttributionCustom(TypedDict, total=False):
    """Custom line written as given (mrkdwn, at most 3,000 effective characters).

    ``{self}`` becomes the sending account's mention:
    ``{"text": ":sparkles: drafted by *Paimon*, reviewed by {self}"}``.
    """

    text: Required[str | Callable[[AttributionInfo], str]]
    #: Passed to a ``text`` callable and recorded in the block ID. Default ``"unconfirmed"``.
    review: AttributionReview


Attribution: TypeAlias = AttributionPreset | AttributionCustom
#: Client default. ``review: "confirmed"`` is accepted only per write (checked at runtime).
DefaultAttribution: TypeAlias = Attribution
#: Per-write override: ``False`` disables it, a partial preset is merged over the client default.
AttributionOverride: TypeAlias = Literal[False] | AttributionPreset | AttributionPatch | AttributionCustom
AttributionScope = Literal["client", "write"]


@dataclass(kw_only=True, slots=True, frozen=True)
class RenderedAttribution:
    """The exact line and block a write would carry."""

    #: mrkdwn of the line.
    text: str
    #: ``{"type": "context", "block_id": ..., "elements": [{"type": "mrkdwn", "text": ...}]}``.
    block: dict[str, Any]


ATTRIBUTION_BLOCK_ID = "gabriel.attribution"
DEFAULT_ATTRIBUTION_ICON = ":robot_face:"
_MAX_FIELD = 64
_MAX_TEXT = 3_000
_REVIEWS = frozenset({"confirmed", "unconfirmed", "hidden"})
_ICON = re.compile(r":[a-z0-9_+'-]+:(?::skin-tone-[2-6]:)?", re.IGNORECASE)
_BROADCAST = re.compile(r"<!(?:channel|here|everyone|subteam\^)", re.IGNORECASE)
_LINE_BREAK = re.compile(r"[\r\n]")
_USER_MENTION = re.compile(r"<@([UW][A-Z0-9]+)")


def _invalid(message: str) -> GabrielError:
    return GabrielError("INVALID_ARGUMENT", f"attribution: {message}")


def _is_custom(a: Any) -> TypeGuard[AttributionCustom]:
    return isinstance(a, dict) and a.get("text") is not None


def _escape_mrkdwn(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _field(a: Any, key: str, required: bool) -> str | None:
    value = a.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise _invalid(f"`{key}` must be a string")
    v = _js_trim(value)
    if not v:
        if required:
            raise _invalid(f"`{key}` must not be empty")
        return None
    if _LINE_BREAK.search(v):
        raise _invalid(f"`{key}` must be a single line")
    if len(v) > _MAX_FIELD:
        raise _invalid(f"`{key}` must be at most {_MAX_FIELD} characters")
    return v


def _check_review(a: Any, allow_confirmed: bool) -> None:
    review = a.get("review")
    if review is None:
        return
    if not isinstance(review, str) or review not in _REVIEWS:
        raise _invalid('`review` must be "confirmed", "unconfirmed" or "hidden"')
    if review == "confirmed" and not allow_confirmed:
        raise _invalid('`review: "confirmed"` is per write only — pass it when a person actually reviewed that message')


def _check_text(text: str) -> str:
    if not _js_trim(text):
        raise _invalid("`text` must not be empty")
    if measure_markdown(text).effective > _MAX_TEXT:
        raise _invalid(f"`text` must be at most {_MAX_TEXT} characters (emoji count as {EMOJI_WEIGHT})")
    if _BROADCAST.search(text):
        raise _invalid("`text` must not contain @channel, @here, @everyone or user group mentions")
    return text


def validate_attribution(a: Attribution, scope: AttributionScope = "write") -> None:
    """Validates an attribution; ``scope="client"`` additionally rejects ``review: "confirmed"``."""
    if not isinstance(a, dict):
        raise _invalid("must be an object")
    _check_review(a, scope == "write")
    if _is_custom(a):
        text = a["text"]
        if isinstance(text, str):
            _check_text(text)
        elif not callable(text):
            raise _invalid("`text` must be a string or a function")
        return
    _field(a, "name", True)
    _field(a, "role", False)
    if "icon" in a:
        icon = a.get("icon")
        if icon is not None and (not isinstance(icon, str) or not _ICON.fullmatch(icon)):
            raise _invalid('`icon` must be an emoji shortcode such as ":robot_face:", or null')


def resolve_attribution(base: DefaultAttribution | None, override: AttributionOverride | None) -> Attribution | None:
    """Applies a per-write override to the client default. Returns ``None`` when attribution is off.

    ``override=None`` means "no override" (use the default); ``False`` turns attribution off.
    """
    if override is False:
        return None
    if override is None:
        return base
    if not isinstance(override, dict):
        raise _invalid("must be an object")
    if _is_custom(override):
        return override
    patch = cast(AttributionPatch, override)
    if base is not None and _is_custom(base) and patch.get("name") is None:
        if patch.get("review") is None:
            return base
        return cast(AttributionCustom, {**base, "review": patch["review"]})
    merged: dict[str, Any] = {**(base if base is not None and not _is_custom(base) else {}), **patch}
    if merged.get("name") is None:
        raise _invalid("`name` is required (set it in the client's `attribution` option or on this write)")
    return cast(AttributionPreset, merged)


def render_attribution(
    a: Attribution, *, self_user_id: str, channel: str, thread_ts: str | None = None
) -> RenderedAttribution:
    """Renders the line and block a write would carry.

    ``render_attribution({"name": "Paimon", "role": "Assistant", "review": "confirmed"},
    self_user_id="U0ALICE0001", channel="C0GENERAL01").text`` →
    ``":robot_face: Paimon (Assistant) · confirmed by <@U0ALICE0001>"``.
    """
    validate_attribution(a)
    review: AttributionReview = a.get("review") or "unconfirmed"
    self_mention = f"<@{self_user_id}>"
    if _is_custom(a):
        text_opt = a["text"]
        if callable(text_opt):
            raw: Any = text_opt(
                AttributionInfo(self_user_id=self_user_id, channel=channel, thread_ts=thread_ts or None, review=review)
            )
        else:
            raw = text_opt
        if not isinstance(raw, str):
            raise _invalid("`text` function must return a string")
        text = _check_text(raw.replace("{self}", self_mention))
        block_suffix = "custom"
    else:
        preset = cast(AttributionPreset, a)
        icon = preset.get("icon", DEFAULT_ATTRIBUTION_ICON)
        role = _field(preset, "role", False)
        name = cast(str, _field(preset, "name", True))
        head = _escape_mrkdwn(name) + (f" ({_escape_mrkdwn(role)})" if role else "")
        if review == "confirmed":
            suffix = f" · confirmed by {self_mention}"
        elif review == "unconfirmed":
            suffix = f" · sent for {self_mention} · not reviewed"
        else:
            suffix = ""
        text = f"{icon} " if icon else ""
        text += head + suffix
        block_suffix = review
    block = {
        "type": "context",
        "block_id": f"{ATTRIBUTION_BLOCK_ID}.{block_suffix}",
        "elements": [{"type": "mrkdwn", "text": text}],
    }
    return RenderedAttribution(text=text, block=block)


def _is_attribution_block(block: Any) -> TypeGuard[dict[str, Any]]:
    if not isinstance(block, dict):
        return False
    block_id = block.get("block_id")
    if block.get("type") != "context" or not isinstance(block_id, str):
        return False
    return block_id == ATTRIBUTION_BLOCK_ID or block_id.startswith(f"{ATTRIBUTION_BLOCK_ID}.")


def split_attribution(blocks: Any) -> tuple[dict[str, Any] | None, Any]:
    """Splits a leading attribution block off a message's ``blocks``: ``(block | None, rest)``."""
    if not isinstance(blocks, list) or not blocks or not _is_attribution_block(blocks[0]):
        return None, blocks
    return blocks[0], blocks[1:]


def attribution_user_ids(message: dict[str, Any]) -> list[str]:
    """User IDs mentioned in a message's attribution line (rendered as names, not mentions)."""
    block, _ = split_attribution(message.get("blocks"))
    if block is None:
        return []
    elements = block.get("elements")
    serialized = json.dumps(elements if elements is not None else [], ensure_ascii=False)
    return _USER_MENTION.findall(serialized)


def parse_attribution(blocks: Any, r: EntityResolver | None = None) -> MessageAttribution | None:
    """Parses a received message's attribution, if its first block is one."""
    block, _ = split_attribution(blocks)
    if block is None:
        return None
    suffix = str(block.get("block_id"))[len(ATTRIBUTION_BLOCK_ID) + 1 :]
    review = cast(AttributionReview, suffix) if suffix in _REVIEWS else None
    return MessageAttribution(text=convert_blocks_to_markdown([block], r), review=review)


# JavaScript `^` in multiline mode: start of input or right after a line terminator.
_BOL = "(?<![^\\n\\r\\u2028\\u2029])"
_FENCE = re.compile(_BOL + r" {0,3}(?:`{3,}|~{3,})[^\n]*\n?")
_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)" + _JS_WS + r"]+)[^)]*\)")
_HEADING = re.compile(_BOL + r" {0,3}#{1,6}[" + _JS_WS + "]+")
_QUOTE = re.compile(_BOL + r" {0,3}>[" + _JS_WS + "]?")
_EMPHASIS = re.compile(r"(\*\*|__|~~)(?=[^" + _JS_WS + r"])([\s\S]*?[^" + _JS_WS + r"])\1")
_BLANK_LINES = re.compile(r"\n{3,}")


def markdown_to_plain_text(markdown: str, max_length: int = _MAX_TEXT) -> str:
    """Plain-text fallback (``text`` field) for a Markdown body sent as blocks.

    Used for notifications and search snippets.
    """
    plain = _FENCE.sub("", markdown)
    plain = _LINK.sub(r"\1", plain)
    plain = _HEADING.sub("", plain)
    plain = _QUOTE.sub("", plain)
    plain = _EMPHASIS.sub(r"\2", plain)
    plain = _js_trim(_BLANK_LINES.sub("\n\n", plain))
    return plain if len(plain) <= max_length else f"{plain[: max_length - 1]}…"
