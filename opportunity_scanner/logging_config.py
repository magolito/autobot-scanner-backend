"""
Logging configuration — driven by settings.yaml's `logging:` section
(level, format). Call `setup_logging(settings)` once at process startup
(api.py, main.py, scheduler.py's run_standalone all do this) rather than
each module configuring its own logger independently.

Two formats: `text` for local development/reading in a terminal, `json`
for production (structured logs that a log aggregator — Railway/Render's
built-in log viewer, or an external service — can parse and filter on).
No external dependency for JSON formatting — Python's stdlib `logging`
plus a small custom Formatter covers it without pulling in
python-json-logger for what's a fairly simple need.
"""

from __future__ import annotations
import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def setup_logging(level: str = "INFO", fmt: str = "text") -> None:
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # clear any existing handlers so repeated calls (e.g. in tests) don't
    # duplicate log lines
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            fmt="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    root.addHandler(handler)


def setup_logging_from_settings(settings) -> None:
    """Convenience wrapper — pass the loaded Settings object directly."""
    setup_logging(level=settings.logging.level, fmt=settings.logging.format)
