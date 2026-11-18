"""Append-only audit journal: who changed what, with what result."""

from __future__ import annotations

import json
import sys
from enum import StrEnum

import psycopg

from core import db
from core.logging import get_logger

logger = get_logger(__name__)


class AuditOutcome(StrEnum):
    """How an attempt to change an account or reach an operator ended."""

    EXECUTED = "executed"
    DUPLICATE_SUPPRESSED = "duplicate_suppressed"
    REJECTED_BY_USER = "rejected_by_user"
    REJECTED_BY_CONTRACT = "rejected_by_contract"
    EXPIRED = "expired"
    ESCALATED = "escalated"
    DENIED = "denied"
    GUARDRAIL_UNSAFE = "guardrail_unsafe"
    GUARDRAIL_DEGRADED = "guardrail_degraded"
    TOKEN_REFRESHED = "token_refreshed"
    SERVER_LIMIT = "server_limit"
    BUDGET_EXHAUSTED = "budget_exhausted"
    ERROR = "error"


# The only outcomes the journal accepts.
OUTCOMES = frozenset(AuditOutcome)


def to_json(value: object) -> str | None:
    """Return the value as JSON text, or None for SQL NULL."""
    return None if value is None else json.dumps(value, ensure_ascii=False)


def append(
    who: str,
    what: str,
    outcome: str,
    *,
    trace_id: str | None = None,
    session_id: str | None = None,
    args: dict | None = None,
    before: object = None,
    after: object = None,
    idempotency_key: str | None = None,
    detail: str | None = None,
) -> None:
    """Add an entry to the journal; a database error is logged, not raised."""
    if outcome not in OUTCOMES:
        raise ValueError(f"unknown audit outcome: {outcome}")
    try:
        db.execute(
            """
            insert into audit_log (
                trace_id, session_id, who, what, outcome,
                args, before, after, idempotency_key, detail
            )
            values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                trace_id,
                session_id,
                who,
                what,
                outcome,
                to_json(args),
                to_json(before),
                to_json(after),
                idempotency_key,
                detail,
            ),
        )
    except psycopg.Error as error:
        # The entry is about something that already happened, so the caller
        # goes on. Every field is logged: the entry can be restored from here.
        logger.error(
            "audit_write_failed",
            error_type=type(error).__name__,
            who=who,
            what=what,
            outcome=outcome,
            trace_id=trace_id,
            session_id=session_id,
            args=args,
            before=before,
            after=after,
            idempotency_key=idempotency_key,
            detail=detail,
        )


def tail(limit: int = 20, who: str | None = None) -> list[dict]:
    """Return the newest entries, of one subscriber if given, oldest first."""
    # A NULL `who` matches every entry.
    newest_entries = db.fetchall(
        """
        select *
        from audit_log
        where %s::text is null or who = %s
        order by id desc
        limit %s
        """,
        (who, who, limit),
    )
    return list(reversed(newest_entries))


def find_card_entries(idempotency_key: str) -> list[dict]:
    """Return the entries with this idempotency key, oldest first."""
    return db.fetchall(
        """
        select *
        from audit_log
        where idempotency_key = %s
        order by id
        """,
        (idempotency_key,),
    )


def trace(trace_id: str) -> list[dict]:
    """Return the trace's audit entries, LLM calls and tool calls by time.

    Each item is a table row plus `source`, the table's name. An audit entry
    also has `card_trace_id`: the trace that created its card, if any.
    """
    audit_entries = db.fetchall(
        """
        select audit_log.*, pending_cards.trace_id as card_trace_id
        from audit_log
        left join pending_cards
            on audit_log.idempotency_key
                = pending_cards.action || ':' || pending_cards.pending_id
        where audit_log.trace_id = %s
        """,
        (trace_id,),
    )
    llm_calls = db.fetchall(
        """
        select *
        from llm_calls
        where trace_id = %s
        """,
        (trace_id,),
    )
    tool_calls = db.fetchall(
        """
        select *
        from tool_calls
        where trace_id = %s
        """,
        (trace_id,),
    )
    timeline = [
        *({**entry, "source": "audit_log"} for entry in audit_entries),
        *({**llm_call, "source": "llm_calls"} for llm_call in llm_calls),
        *({**tool_call, "source": "tool_calls"} for tool_call in tool_calls),
    ]
    return sorted(timeline, key=lambda item: (item["ts"], item["id"]))


def find_newest_trace_id() -> str | None:
    """Return the trace id of the newest row of the three tables."""
    newest_row = db.fetchone(
        """
        select trace_id
        from (
            select trace_id, ts from audit_log
            union all
            select trace_id, ts from llm_calls
            union all
            select trace_id, ts from tool_calls
        ) as trace_rows
        where trace_id is not null
        order by ts desc
        limit 1
        """
    )
    return newest_row["trace_id"] if newest_row else None


def format_value(value: object) -> str:
    """Return a stored value as text: a string as it is, the rest as JSON."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def format_trace_item(item: dict) -> str:
    """Return one line of a printed trace."""
    if item["source"] == "llm_calls":
        fields = [
            str(item["agent"]),
            f"in={item['input_tokens']} out={item['output_tokens']}",
            f"{item['latency_ms']} ms",
        ]
    elif item["source"] == "tool_calls":
        fields = [
            item["tool"],
            str(item["outcome"]),
            format_value(item["args"]),
            f"{item['latency_ms']} ms",
        ]
    else:
        fields = [item["outcome"], item["what"]]
        # `before` may be False, so compare with None.
        if item["before"] is not None or item["after"] is not None:
            fields.append(
                f"{format_value(item['before'])}"
                f" → {format_value(item['after'])}"
            )
        if item["detail"]:
            fields.append(item["detail"])
        if item["idempotency_key"]:
            fields.append(f"key={item['idempotency_key']}")
    time_of_day = item["ts"].astimezone().strftime("%H:%M:%S")
    return f"{time_of_day} {item['source']:<10} " + "  ".join(fields)


def find_card_history(idempotency_key: str, card_trace_id: str) -> list[dict]:
    """Return the items of the turn that created the card and its entries."""
    # The entries come from every turn that answered the card.
    card_entries = [
        {**entry, "source": "audit_log"}
        for entry in find_card_entries(idempotency_key)
    ]
    return [*trace(card_trace_id), *card_entries]


def add_card_histories(timeline: list[dict]) -> list[dict]:
    """Return the timeline with the history of each card it is about.

    Every item appears once, and all of them are in time order.
    """
    card_trace_by_key = {
        item["idempotency_key"]: item["card_trace_id"]
        for item in timeline
        if item["source"] == "audit_log" and item["card_trace_id"]
    }
    # An entry of this turn about the card is in its history too: the key of
    # a row, its table and id, keeps it from appearing twice.
    item_by_row = {(item["source"], item["id"]): item for item in timeline}
    for idempotency_key, card_trace_id in card_trace_by_key.items():
        for item in find_card_history(idempotency_key, card_trace_id):
            item_by_row.setdefault((item["source"], item["id"]), item)
    return sorted(
        item_by_row.values(), key=lambda item: (item["ts"], item["id"])
    )


def print_trace(trace_id: str | None) -> None:
    """Print a trace, or the newest one when no id is given.

    A turn that answered a card shows only the answer, while the card's story
    spans several turns. So the card's whole history is printed with it: the
    turn that created the card and every answer to it, each under its turn.
    """
    trace_id = trace_id or find_newest_trace_id()
    if trace_id is None:
        print("no traces yet")
        return
    print(f"trace {trace_id}")
    items = add_card_histories(trace(trace_id))
    shows_several_turns = len({item["trace_id"] for item in items}) > 1
    shown_turn_id = None
    for item in items:
        if shows_several_turns and item["trace_id"] != shown_turn_id:
            shown_turn_id = item["trace_id"]
            print(f"turn {shown_turn_id}")
        print(format_trace_item(item))


if __name__ == "__main__":
    print_trace(sys.argv[1] if len(sys.argv) > 1 else None)
