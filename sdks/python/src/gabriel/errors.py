"""Unified error model. Every public API raises a :class:`GabrielError`.

``retryable is True`` means the error belongs to a transient class. The SDK has already
retried those internally, so when one reaches application code the automatic retries are
exhausted — do not wrap SDK calls in another retry loop.
"""

from __future__ import annotations

from typing import Literal

GabrielErrorCode = Literal[
    "RATE_LIMITED",
    "NETWORK_TIMEOUT",
    "SOCKET_CLOSED",
    "SERVER_ERROR",
    "MESSAGE_NOT_FOUND_TRANSIENT",
    "TOKEN_EXPIRED",
    "AUTH_DEAD",
    "NOT_IN_CHANNEL",
    "CHANNEL_NOT_FOUND",
    "PERMISSION_DENIED",
    "MSG_TOO_LONG",
    "MSG_BLOCKS_TOO_LONG",
    "SUBSCRIPTION_CAP_EXCEEDED",
    "SCOPE_POLICY_VIOLATION",
    "FILE_TOO_LARGE",
    "HANDLER_TIMEOUT",
    "CANCELLED",
    "STREAM_CLOSED",
    "QUEUE_OVERFLOW",
    "STATE_STORE_UNAVAILABLE",
    # Codes below are additions of the reference implementation (see sdks/node/README.md).
    "LOOP_DETECTED",
    "INVALID_ARGUMENT",
    "INVALID_STATE",
    "SLACK_API_ERROR",
]

_RETRYABLE: frozenset[str] = frozenset(
    {
        "RATE_LIMITED",
        "NETWORK_TIMEOUT",
        "SOCKET_CLOSED",
        "SERVER_ERROR",
        "MESSAGE_NOT_FOUND_TRANSIENT",
        "TOKEN_EXPIRED",
    }
)


class GabrielError(Exception):
    """The single exception type raised by the SDK.

    Attributes:
        code: Stable machine-readable error code.
        retryable: True for transient classes (already retried internally).
        raw_slack_error: Slack's original error string, e.g. ``"channel_not_found"``.
        retry_after: Suggested cool-down in seconds when rate limited.
    """

    code: GabrielErrorCode
    retryable: bool
    raw_slack_error: str | None
    retry_after: float | None

    def __init__(
        self,
        code: GabrielErrorCode,
        message: str,
        *,
        raw_slack_error: str | None = None,
        retry_after: float | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = code in _RETRYABLE
        self.raw_slack_error = raw_slack_error
        self.retry_after = retry_after
        if cause is not None:
            self.__cause__ = cause

    @property
    def message(self) -> str:
        return str(self)

    def __repr__(self) -> str:
        return f"GabrielError({self.code!r}, {str(self)!r})"


def is_gabriel_error(err: object, code: GabrielErrorCode | None = None) -> bool:
    return isinstance(err, GabrielError) and (code is None or err.code == code)


#: Slack error strings that mean "the workspace token is no longer valid".
TOKEN_INVALID_ERRORS: frozenset[str] = frozenset({"invalid_auth", "not_authed", "token_expired", "token_revoked"})

_SLACK_ERROR_MAP: dict[str, GabrielErrorCode] = {
    "ratelimited": "RATE_LIMITED",
    "rate_limited": "RATE_LIMITED",
    "channel_not_found": "CHANNEL_NOT_FOUND",
    "not_in_channel": "NOT_IN_CHANNEL",
    "is_archived": "PERMISSION_DENIED",
    "restricted_action": "PERMISSION_DENIED",
    "not_allowed_token_type": "PERMISSION_DENIED",
    "cant_update_message": "PERMISSION_DENIED",
    "cant_delete_message": "PERMISSION_DENIED",
    "cant_delete_file": "PERMISSION_DENIED",
    "access_denied": "PERMISSION_DENIED",
    "missing_scope": "PERMISSION_DENIED",
    "no_permission": "PERMISSION_DENIED",
    "account_inactive": "AUTH_DEAD",
    "msg_too_long": "MSG_TOO_LONG",
    "msg_blocks_too_long": "MSG_BLOCKS_TOO_LONG",
    "internal_error": "SERVER_ERROR",
    "fatal_error": "SERVER_ERROR",
    "service_unavailable": "SERVER_ERROR",
    "request_timeout": "NETWORK_TIMEOUT",
}


def from_slack_error(method: str, slack_error: str) -> GabrielError:
    """Maps a Slack ``{"ok": false, "error": ...}`` string to a :class:`GabrielError`."""
    if slack_error in TOKEN_INVALID_ERRORS:
        return GabrielError("TOKEN_EXPIRED", f"{method}: {slack_error}", raw_slack_error=slack_error)
    code = _SLACK_ERROR_MAP.get(slack_error, "SLACK_API_ERROR")
    return GabrielError(code, f"{method}: {slack_error}", raw_slack_error=slack_error)


def cancelled_error(reason: object = None) -> GabrielError:
    if isinstance(reason, GabrielError):
        return reason
    if isinstance(reason, str):
        text = reason
    elif isinstance(reason, BaseException) and str(reason):
        text = str(reason)
    else:
        text = "operation cancelled"
    return GabrielError("CANCELLED", text, cause=reason if isinstance(reason, BaseException) else None)
