"""Language-neutral conformance fixtures (port of sdks/node/test/conformance.test.ts).

Raw frame → normalized event (after rendering with the fixture's entity names). Every produced
event is also validated against spec/schema/events.schema.json.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import jsonschema
import pytest

from gabriel.events.normalize import Draft, NormalizeContext, normalize_frame
from gabriel.markdown.blocks_to_markdown import EntityResolver

SPEC = Path(__file__).resolve().parents[3] / "spec"
FIXTURES = sorted((SPEC / "fixtures" / "normalize").glob("*.json"))


@cache
def _validator() -> Any:
    schema = json.loads((SPEC / "schema" / "events.schema.json").read_text("utf-8"))
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def _load(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return data


def _normalize(fx: dict[str, Any]) -> Draft | None:
    ctx = fx["context"]
    my_subteams: list[str] = ctx["mySubteams"]
    return normalize_frame(
        fx["frame"],
        NormalizeContext(self_user_id=ctx["selfUserId"], is_my_subteam=lambda sid: sid in my_subteams),
    )


def _render(draft: Draft, fx: dict[str, Any]) -> dict[str, Any]:
    e = fx["context"]["entities"]
    draft.render(
        EntityResolver(user_name=e["users"].get, channel_name=e["channels"].get, usergroup_handle=e["usergroups"].get)
    )
    # Serializable payload (JSON round-trip, like JSON.parse(JSON.stringify(event)) in Node).
    payload: dict[str, Any] = json.loads(draft.event.to_json())
    return payload


def _match_object(actual: Any, expected: Any, path: str = "$") -> None:
    """Jest ``toMatchObject``: objects match as subsets (recursively), arrays element-wise."""
    if isinstance(expected, dict):
        assert isinstance(actual, dict), f"{path}: expected object, got {actual!r}"
        for k, v in expected.items():
            assert k in actual, f"{path}.{k}: missing"
            _match_object(actual[k], v, f"{path}.{k}")
    elif isinstance(expected, list):
        assert isinstance(actual, list), f"{path}: expected array, got {actual!r}"
        assert len(actual) == len(expected), f"{path}: length {len(actual)} != {len(expected)}"
        for i, (a, e) in enumerate(zip(actual, expected, strict=True)):
            _match_object(a, e, f"{path}[{i}]")
    else:
        assert type(actual) is type(expected) or (
            isinstance(actual, int | float)
            and isinstance(expected, int | float)
            and not isinstance(actual, bool)
            and not isinstance(expected, bool)
        ), f"{path}: {actual!r} != {expected!r}"
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


def test_has_fixtures() -> None:
    assert len(FIXTURES) > 10


@pytest.mark.parametrize("path", FIXTURES, ids=[p.name for p in FIXTURES])
def test_conformance_normalize(path: Path) -> None:
    fx = _load(path)
    draft = _normalize(fx)
    if fx["expected"] is None:
        assert draft is None
        return
    assert draft is not None
    _match_object(_render(draft, fx), fx["expected"])


@pytest.mark.parametrize("path", FIXTURES, ids=[p.name for p in FIXTURES])
def test_fixture_events_match_schema(path: Path) -> None:
    fx = _load(path)
    draft = _normalize(fx)
    if draft is None:
        return
    _validator().validate(_render(draft, fx))


# Additional frames covering event kinds and optional fields the fixtures do not exercise.
_CTX = {
    "selfUserId": "U0SELF00001",
    "mySubteams": ["S0ONCALL001"],
    "entities": {
        "users": {"U0ALICE0001": "Alice", "U0SELF00001": "Me"},
        "channels": {"C0GENERAL01": "general"},
        "usergroups": {"S0ONCALL001": "oncall"},
    },
}
_EXTRA_FRAMES: list[tuple[str, dict[str, Any]]] = [
    (
        "message-files-attachments",
        {
            "type": "message",
            "subtype": "file_share",
            "channel": "C0GENERAL01",
            "user": "U0ALICE0001",
            "text": "see file",
            "ts": "1800000020.000100",
            "files": [
                {"id": "F0FILE00001", "name": "a.txt", "mimetype": "text/plain", "url_private": "https://files.example/a",
                 "size": 12, "title": "A", "permalink": "https://your-workspace.slack.com/files/F0FILE00001"},
                {"name": "no id"},
            ],
            "attachments": [{"pretext": "pre", "text": "<@U0ALICE0001> body"}, {"fallback": "fb"}],
        },
    ),
    (
        "ai-stream-completed",
        {
            "type": "message",
            "subtype": "message_changed",
            "channel": "C0GENERAL01",
            "ts": "1800000021.000200",
            "message": {
                "type": "message",
                "bot_id": "B0AIBOT0001",
                "bot_profile": {"id": "B0AIBOT0001", "name": "Helper", "app_id": "A0APP000001"},
                "text": "final answer",
                "ts": "1800000007.000100",
            },
            "previous_message": {"text": "partial", "streaming_state": "in_progress", "ts": "1800000007.000100"},
        },
    ),
    (
        "message-changed-no-previous",
        {
            "type": "message",
            "subtype": "message_changed",
            "channel": "C0GENERAL01",
            "ts": "1800000022.000200",
            "message": {"type": "message", "user": "U0SELF00001", "text": "edited", "ts": "1800000006.000100",
                        "thread_ts": "1800000000.000100"},
        },
    ),
    (
        "message-deleted-thread",
        {
            "type": "message",
            "subtype": "message_deleted",
            "channel": "C0GENERAL01",
            "deleted_ts": "1800000002.000100",
            "ts": "1800000023.000100",
            "previous_message": {"text": "gone", "thread_ts": "1800000000.000100", "ts": "1800000002.000100"},
        },
    ),
    (
        "reaction-removed-file",
        {"type": "reaction_removed", "user": "U0ALICE0001", "reaction": "x",
         "item": {"type": "file", "file": "F0FILE00001"}, "event_ts": "1800000024.000100"},
    ),
    ("message-other-no-channel", {"type": "message", "subtype": "bot_add", "user": "U0ALICE0001"}),
    (
        "activity",
        {
            "type": "activity",
            "key": "k1",
            "event_ts": "1800000025.000100",
            "entry": {
                "is_unread": True,
                "feed_ts": "1800000025.000000",
                "item": {"type": "at_user", "message": {"channel": "C0GENERAL01", "ts": "1800000002.000100",
                                                        "thread_ts": "1800000000.000100"}},
            },
        },
    ),
    (
        "subteam-updated",
        {"type": "subteam_updated", "subteam": {"id": "S0ONCALL001", "handle": "oncall",
                                                "users": ["U0ALICE0001", 3], "date_update": 1800000026}},
    ),
    (
        "im-created",
        {"type": "im_created", "user": "U0ALICE0001", "channel": {"id": "D0DIRECT001"},
         "event_ts": "1800000027.000100"},
    ),
    (
        "group-rename",
        {"type": "group_rename",
         "channel": {"id": "G0PRIVATE01", "name": "secret", "is_private": True, "is_mpim": False}},
    ),
    ("badge-counts-empty", {"type": "badge_counts_updated"}),
]  # fmt: skip


@pytest.mark.parametrize(("name", "frame"), _EXTRA_FRAMES, ids=[n for n, _ in _EXTRA_FRAMES])
def test_extra_events_match_schema(name: str, frame: dict[str, Any]) -> None:
    fx = {"context": _CTX, "frame": frame}
    draft = _normalize(fx)
    assert draft is not None, name
    _validator().validate(_render(draft, fx))
