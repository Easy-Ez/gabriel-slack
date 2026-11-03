"""Structured logger contract.

The SDK logs ``(fields, msg)`` pairs, like pino / structlog. Pass any object with
``debug/info/warn/error(fields, msg=None)`` methods, or wrap a stdlib :class:`logging.Logger`
with :func:`stdlib_logger` (the default, using the ``"gabriel"`` logger). The SDK never passes
credentials or message bodies to the logger.
"""

from __future__ import annotations

import json
import logging
import sys
from typing import Any, Literal, Protocol

Level = Literal["debug", "info", "warn", "error"]


class Logger(Protocol):
    def debug(self, fields: dict[str, Any], msg: str | None = None) -> None: ...
    def info(self, fields: dict[str, Any], msg: str | None = None) -> None: ...
    def warn(self, fields: dict[str, Any], msg: str | None = None) -> None: ...
    def error(self, fields: dict[str, Any], msg: str | None = None) -> None: ...


def _format(fields: dict[str, Any], msg: str | None) -> str:
    try:
        body = json.dumps(fields, default=str, ensure_ascii=False)
    except (TypeError, ValueError):
        body = repr(fields)
    return f"{msg or ''} {body}".strip()


class _Silent:
    def debug(self, fields: dict[str, Any], msg: str | None = None) -> None:
        pass

    info = warn = error = debug


silent_logger: Logger = _Silent()


class _Stdlib:
    def __init__(self, logger: logging.Logger) -> None:
        self._logger = logger

    def _log(self, level: int, fields: dict[str, Any], msg: str | None) -> None:
        if self._logger.isEnabledFor(level):
            self._logger.log(level, "%s", _format(fields, msg), extra={"gabriel": fields})

    def debug(self, fields: dict[str, Any], msg: str | None = None) -> None:
        self._log(logging.DEBUG, fields, msg)

    def info(self, fields: dict[str, Any], msg: str | None = None) -> None:
        self._log(logging.INFO, fields, msg)

    def warn(self, fields: dict[str, Any], msg: str | None = None) -> None:
        self._log(logging.WARNING, fields, msg)

    def error(self, fields: dict[str, Any], msg: str | None = None) -> None:
        self._log(logging.ERROR, fields, msg)


def stdlib_logger(logger: logging.Logger | None = None) -> Logger:
    """Adapts a stdlib logger (default ``logging.getLogger("gabriel")``)."""
    return _Stdlib(logger or logging.getLogger("gabriel"))


_ORDER: tuple[Level, ...] = ("debug", "info", "warn", "error")


class _Console:
    def __init__(self, level: Level) -> None:
        self._min = _ORDER.index(level)

    def _emit(self, lvl: Level, fields: dict[str, Any], msg: str | None) -> None:
        if _ORDER.index(lvl) < self._min:
            return
        stream = sys.stderr if lvl in ("warn", "error") else sys.stdout
        print(f"[gabriel] {lvl.upper()} {_format(fields, msg)}", file=stream)

    def debug(self, fields: dict[str, Any], msg: str | None = None) -> None:
        self._emit("debug", fields, msg)

    def info(self, fields: dict[str, Any], msg: str | None = None) -> None:
        self._emit("info", fields, msg)

    def warn(self, fields: dict[str, Any], msg: str | None = None) -> None:
        self._emit("warn", fields, msg)

    def error(self, fields: dict[str, Any], msg: str | None = None) -> None:
        self._emit("error", fields, msg)


def console_logger(level: Level = "info") -> Logger:
    """A print-backed logger, handy for examples and local debugging."""
    return _Console(level)
