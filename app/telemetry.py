"""Telemetry: one row per agent run and per tool call a model makes."""

from __future__ import annotations

import json
from typing import LiteralString

import psycopg

from core import db
from core.logging import get_logger

logger = get_logger(__name__)

# Long arguments, such as a ticket note, retell the subscriber's words.
TOOL_ARG_MAX_CHARS = 100


def insert_row(table: str, sql: LiteralString, params: tuple) -> None:
    """Insert one row; a database error is logged, not raised."""
    try:
        db.execute(sql, params)
    except psycopg.Error as error:
        # A lost telemetry row is acceptable: the turn goes on.
        logger.warning(
            "telemetry_write_failed",
            table=table,
            error_type=type(error).__name__,
        )


def cut_long_strings(args: dict) -> dict:
    """Return the arguments with top-level strings cut to TOOL_ARG_MAX_CHARS."""
    return {
        argument_name: (
            argument_value[:TOOL_ARG_MAX_CHARS]
            if isinstance(argument_value, str)
            else argument_value
        )
        for argument_name, argument_value in args.items()
    }


def record_llm_call(
    trace_id: str | None,
    agent: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    latency_ms: int,
) -> None:
    """Record one agent run: its model, tokens and latency."""
    insert_row(
        "llm_calls",
        """
        insert into llm_calls (
            trace_id, agent, model, input_tokens, output_tokens, latency_ms
        )
        values (%s, %s, %s, %s, %s, %s)
        """,
        (trace_id, agent, model, input_tokens, output_tokens, latency_ms),
    )


def record_tool_call(
    trace_id: str | None, tool: str, args: dict, outcome: str, latency_ms: int
) -> None:
    """Record one tool call with its arguments cut short."""
    insert_row(
        "tool_calls",
        """
        insert into tool_calls (trace_id, tool, args, outcome, latency_ms)
        values (%s, %s, %s, %s, %s)
        """,
        (
            trace_id,
            tool,
            json.dumps(cut_long_strings(args), ensure_ascii=False),
            outcome,
            latency_ms,
        ),
    )
