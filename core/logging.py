"""Structured console logging shared by every process of the system."""

import uuid
from functools import cache

import structlog

from core import config

# The longest text a debug line shows: knowledge base articles are long.
DEBUG_TEXT_MAX_CHARS = 2000


@cache
def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            # simple local ts
            structlog.processors.TimeStamper(fmt="%H:%M:%S", utc=False),
            # no padding
            structlog.dev.ConsoleRenderer(pad_event_to=0, pad_level=False),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(config.LOG_LEVEL),
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    """Return a logger whose lines carry `name`."""
    configure_logging()
    return structlog.get_logger(logger_name=name)


def shorten_for_log(text: str) -> str:
    """Return the text cut to DEBUG_TEXT_MAX_CHARS, marked if it was cut."""
    if len(text) <= DEBUG_TEXT_MAX_CHARS:
        return text
    return f"{text[:DEBUG_TEXT_MAX_CHARS]}… [{len(text)} chars]"


def make_trace_id() -> str:
    """Return a new trace id, such as tr_1a2b3c4d5e6f."""
    return f"tr_{uuid.uuid4().hex[:12]}"


def read_trace_id() -> str | None:
    """Return the trace id bound to the log context, if any."""
    return structlog.contextvars.get_contextvars().get("trace_id")
