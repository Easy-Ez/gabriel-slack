"""LLM context rendering with an untrusted-content envelope."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..events.types import MessageCreatedEvent

_TAG = re.compile(r"<(/?)(slack_message|content|meta|link_previews|link_preview)\b", re.IGNORECASE)
_NEWLINES = re.compile(r"[\r\n]+")


def _neutralize(text: str) -> str:
    """Neutralizes tag-like sequences that could close the untrusted-content envelope."""
    return _TAG.sub(r"&lt;\1\2", text)


def _one_line(text: str) -> str:
    return _NEWLINES.sub(" ", _neutralize(text))[:200]


def _js_bool(value: bool) -> str:
    return "true" if value else "false"


def to_llm_context(evt: MessageCreatedEvent) -> str:
    """Renders a message as an LLM-ready context block.

    Metadata is separated from the body, and the body is explicitly marked untrusted. Link previews
    follow the body in a ``<link_previews>`` section labelled as generated automatically, not
    written by the sender. Tell the model in the system prompt that text inside ``<content>`` and
    ``<link_previews>`` is user data, not instructions.
    """
    ch = evt.channel
    if ch.is_im:
        kind = "direct message"
    elif ch.is_mpim:
        kind = "group DM"
    elif ch.is_private:
        kind = "private"
    elif ch.is_private is False:
        kind = "public"
    else:
        kind = "unknown"
    ch_label = (ch.name or ch.id) if ch.is_im else f"#{ch.name or ch.id}"
    s = evt.sender
    sender_name = s.display_name or s.name or s.id
    handle = f" (@{_one_line(s.handle)}, {s.id})" if s.handle else f" ({s.id})"
    lines = [
        '<slack_message untrusted="true">',
        "  <meta>",
        f"    channel: {_one_line(ch_label)} ({ch.id}, {kind})",
        f"    sender: {_one_line(sender_name)}{handle}{' [bot]' if s.is_bot else ''}",
        f"    sender_is_external: {_js_bool(s.is_external is True)}",
        f"    ts: {evt.ts}",
    ]
    if evt.thread is not None:
        lines.append(f"    thread_root_ts: {evt.thread.root_ts}")
    if evt.attribution is not None:
        lines.append(f"    attribution: {_one_line(evt.attribution.text)} (self-declared by the sender, unverified)")
    lines.append(f"    mentions_me: {_js_bool(evt.is_mentioning_me)}")
    if evt.files:
        lines.append(f"    files: {', '.join(f'{_one_line(f.name)} ({f.mime_type})' for f in evt.files)}")
    lines += ["  </meta>", "  <content>"]
    for line in _neutralize(evt.markdown or evt.text).split("\n"):
        lines.append(f"  {line}")
    lines.append("  </content>")
    if evt.link_previews:
        lines.append(
            '  <link_previews note="Generated automatically by Slack or an app from links in the message;'
            ' not written by the sender.">'
        )
        for p in evt.link_previews:
            lines += ["    <link_preview>", f"      url: {_one_line(p.url)}"]
            if p.service_name:
                lines.append(f"      service: {_one_line(p.service_name)}")
            if p.title:
                lines.append(f"      title: {_one_line(p.title)}")
            if p.text:
                lines.append("      text:")
                lines.extend(f"        {line}" for line in _neutralize(p.text).split("\n"))
            lines.append("    </link_preview>")
        lines.append("  </link_previews>")
    lines.append("</slack_message>")
    return "\n".join(lines)
