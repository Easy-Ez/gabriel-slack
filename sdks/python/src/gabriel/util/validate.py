"""Argument validation and Slack timestamp helpers."""

from __future__ import annotations

import json
import re
from typing import Any, TypeGuard

from ..errors import GabrielError

_CHANNEL_ID = re.compile(r"^[CDG][A-Z0-9]{2,30}$")
_USER_ID = re.compile(r"^[UWB][A-Z0-9]{2,30}$")
_TEAM_ID = re.compile(r"^[TE][A-Z0-9]{2,30}$")
_FILE_ID = re.compile(r"^F[A-Z0-9]{2,30}$")
_SUBTEAM_ID = re.compile(r"^S[A-Z0-9]{2,30}$")
_TS = re.compile(r"^\d{1,12}\.\d{1,8}$")
_EMOJI = re.compile(r"^[a-z0-9_+\-'’]{1,100}(::skin-tone-[2-6])?$", re.IGNORECASE)

_MAX_SAFE_INTEGER = 2**53 - 1


def _fail(field: str, value: object, expected: str) -> GabrielError:
    shown = json.dumps(value[:40], ensure_ascii=False) if isinstance(value, str) else type(value).__name__
    return GabrielError("INVALID_ARGUMENT", f"{field}: expected {expected}, got {shown}")


def assert_channel_id(value: Any, field: str = "channel") -> str:
    if not isinstance(value, str) or not _CHANNEL_ID.match(value):
        raise _fail(field, value, "a channel ID (C…/G…/D…)")
    return value


def assert_user_id(value: Any, field: str = "user_id") -> str:
    if not isinstance(value, str) or not _USER_ID.match(value):
        raise _fail(field, value, "a user ID (U…/W…)")
    return value


def assert_team_id(value: Any, field: str = "team_id") -> str:
    if not isinstance(value, str) or not _TEAM_ID.match(value):
        raise _fail(field, value, "a team ID (T…/E…)")
    return value


def assert_file_id(value: Any, field: str = "file_id") -> str:
    if not isinstance(value, str) or not _FILE_ID.match(value):
        raise _fail(field, value, "a file ID (F…)")
    return value


def is_subteam_id(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _SUBTEAM_ID.match(value) is not None


def assert_ts(value: Any, field: str = "ts") -> str:
    if not isinstance(value, str) or not _TS.match(value):
        raise _fail(field, value, "a Slack timestamp like 1700000000.000100")
    return value


def is_ts(value: object) -> TypeGuard[str]:
    return isinstance(value, str) and _TS.match(value) is not None


def normalize_emoji(value: Any, field: str = "emoji") -> str:
    """Normalizes ``":eyes:"`` → ``"eyes"`` and validates the emoji name."""
    if not isinstance(value, str):
        raise _fail(field, value, "an emoji name")
    name = value.strip()
    if name.startswith(":"):
        name = name[1:]
    if name.endswith(":"):
        name = name[:-1]
    if not _EMOJI.match(name):
        raise _fail(field, value, 'an emoji name like "eyes"')
    return name


def assert_non_empty_string(value: Any, field: str, max_length: int = 1_000_000) -> str:
    if not isinstance(value, str) or len(value) == 0 or len(value) > max_length:
        raise _fail(field, value, f"a non-empty string (<= {max_length} chars)")
    return value


def assert_positive_int(value: Any, field: str, max: int = _MAX_SAFE_INTEGER) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0 or value > max:
        raise _fail(field, value, f"a positive integer <= {max}")
    return value


def compare_ts(a: str, b: str) -> int:
    """Compares two Slack timestamps ("seconds.micros") numerically without float rounding.

    Returns <0, 0, >0 like a comparator.
    """
    a_sec, _, a_frac = a.partition(".")
    b_sec, _, b_frac = b.partition(".")
    sec_diff = int(a_sec or "0") - int(b_sec or "0")
    if sec_diff != 0:
        return sec_diff
    width = max(len(a_frac), len(b_frac))
    return int(a_frac.ljust(width, "0") or "0") - int(b_frac.ljust(width, "0") or "0")


def max_ts(a: str | None, b: str | None) -> str | None:
    if a is None:
        return b
    if b is None:
        return a
    return a if compare_ts(a, b) >= 0 else b
