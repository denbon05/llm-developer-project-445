"""Structured console logging shared by every process of the system."""

from functools import cache

import structlog

from core import config


@cache
def configure_logging() -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(config.LOG_LEVEL),
    )


def get_logger(name: str) -> structlog.typing.FilteringBoundLogger:
    """Return a logger whose lines carry `name`."""
    configure_logging()
    return structlog.get_logger(logger_name=name)
