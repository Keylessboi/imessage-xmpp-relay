"""Structured logging setup.

Every record is rendered as space separated key=value pairs so a log line can be
grepped or parsed without a JSON-aware shipper. Secrets are never logged: the
Config dataclass keeps the password out of its repr and nothing in this module
ever receives it.
"""
from __future__ import annotations

import logging
import sys

_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName", "message", "asctime",
}


class KeyValueFormatter(logging.Formatter):
    """Render time=... level=... logger=... msg=... plus any structured extras."""

    def format(self, record: logging.LogRecord) -> str:
        parts = [
            "time=" + self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level=" + record.levelname,
            "logger=" + record.name,
            "msg=" + _quote(record.getMessage()),
        ]
        for key, value in sorted(record.__dict__.items()):
            if key in _RESERVED or key.startswith("_"):
                continue
            parts.append(key + "=" + _quote(value))
        if record.exc_info:
            parts.append("exc=" + _quote(self.formatException(record.exc_info)))
        return " ".join(parts)


def _quote(value: object) -> str:
    text = str(value)
    if any(ch in text for ch in ' "=\n'):
        escaped = text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
        return '"' + escaped + '"'
    return text


_RESERVED_KWARGS = {"exc_info", "stack_info", "stacklevel", "extra"}


class _StructuredAdapter(logging.LoggerAdapter):
    """Logger that accepts structured fields as plain keyword arguments.

    logging itself only accepts reserved kwargs, so log.info("msg", event="x")
    would raise TypeError. This adapter folds every non-reserved keyword into
    the record's extra mapping, where KeyValueFormatter renders it.
    """

    def process(self, msg, kwargs):
        extra = dict(kwargs.pop("extra", None) or {})
        for key in list(kwargs):
            if key not in _RESERVED_KWARGS:
                extra[key] = kwargs.pop(key)
        if extra:
            kwargs["extra"] = extra
        return msg, kwargs


def get_logger(name: str, **fields) -> logging.LoggerAdapter:
    """A logger whose extra keyword arguments become structured fields."""
    return _StructuredAdapter(logging.getLogger(name), dict(fields))


def setup_logging(level: str = "INFO", stream=sys.stderr) -> None:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(KeyValueFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if level.upper() != "DEBUG":
        for noisy in ("slixmpp", "aiohttp.access", "aiohttp.server"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
