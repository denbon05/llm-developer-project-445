"""The audit journal and a turn's trace, on the real tables."""

import pytest

from app import audit, cards, orchestrator, telemetry
from core.logging import make_trace_id
from tests.integration.fake_mcp import FakeMcpPool, make_session

SUBSCRIBER_ID = "u_1111"
OTHER_SUBSCRIBER_ID = "u_2222"


def test_trace_lists_a_turns_records_in_time_order():
    """Model runs, tool calls and audit entries; a card entry names its turn."""
    # A card created in an earlier turn, the one of card_session: its row
    # keeps that turn's trace id. Only the trace id matters here; create()
    # also needs details and a view, whatever they are.
    card_session = make_session(FakeMcpPool({}))
    card = cards.create(
        card_session,
        "tariff_change",
        {"tariff": "Семейный"},
        {"Станет": "Семейный"},
    )
    # The turn under test.
    trace_id = make_trace_id()

    # Five records of the turn, in this order: a model run, a tool call, then
    # three audit entries. Only the first entry is about the card.
    telemetry.record_llm_call(trace_id, "planner", "test-model", 812, 64, 1820)
    telemetry.record_tool_call(trace_id, "get_my_tariff", {}, "ok", 95)
    audit.append(
        SUBSCRIBER_ID,
        "tariff_change",
        "executed",
        trace_id=trace_id,
        idempotency_key=orchestrator.build_idempotency_key(card),
    )
    audit.append(
        OTHER_SUBSCRIBER_ID,
        "create_ticket",
        "escalated",
        trace_id=trace_id,
        detail="t_1674",
    )
    audit.append(
        SUBSCRIBER_ID,
        "create_ticket",
        "escalated",
        trace_id=trace_id,
        detail="t_1675",
    )

    timeline = audit.trace(trace_id)

    # The three tables come back as one list, in the order they were written.
    # Only the entry with the card's key points to the turn that created the
    # card; the other entries have None, and telemetry rows have no such field.
    assert [
        (item["source"], item.get("card_trace_id")) for item in timeline
    ] == [
        ("llm_calls", None),
        ("tool_calls", None),
        ("audit_log", card_session.trace_id),
        ("audit_log", None),
        ("audit_log", None),
    ]
    # tail() keeps one subscriber's entries, oldest first: the other
    # subscriber's ticket t_1674 is left out.
    assert [entry["detail"] for entry in audit.tail(who=SUBSCRIBER_ID)] == [
        None,
        "t_1675",
    ]


def test_unknown_outcome_is_refused():
    """The set of outcomes is fixed, so nothing is written."""
    with pytest.raises(ValueError):
        audit.append(SUBSCRIBER_ID, "tariff_change", "approved")

    assert audit.tail() == []
