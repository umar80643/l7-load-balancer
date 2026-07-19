"""Structured logging setup.

Design decision: we use the standard library's `logging` module with a
custom JSON formatter, rather than a third-party library like structlog or
loguru. The stdlib module integrates cleanly with aiohttp's own access logs
and with pytest's log capture, needs no extra dependency, and a JSON
formatter is small enough (~30 lines) that writing our own is simpler than
learning another library's configuration surface.

We emit JSON (not human-readable text) because in production this stream is
meant to be scraped by something like Fluentd/Loki/CloudWatch and queried by
field (backend_url, status_code, algorithm) -- not tailed by a human.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}


class JsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON objects.

    Extra fields passed via `logger.info(msg, extra={...})` are merged into
    the output object, which is how we attach structured context like
    `backend` or `algorithm` to a log line without string-formatting it in.
    """

    # Attributes that exist on every LogRecord by default; anything else set
    # on the record (via `extra=`) is application-supplied structured data
    # we want to surface in the output.
    _RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
        "message",
        "asctime",
    }

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._RESERVED:
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


def new(level: str = "info", name: str = "lb") -> logging.Logger:
    """Build and return a configured Logger writing JSON to stdout.

    Unknown level strings fail safe to INFO -- a typo in a log-level env var
    shouldn't prevent the load balancer from starting.
    """
    logger = logging.getLogger(name)
    logger.setLevel(_LEVELS.get(level.lower(), logging.INFO))
    logger.propagate = False

    # Avoid duplicate handlers if new() is called more than once with the
    # same logger name (e.g. across multiple tests in the same process).
    logger.handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)

    return logger
