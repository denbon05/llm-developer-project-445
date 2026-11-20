"""How code answers a confirmation card, on the real Postgres tables.

A fake MCP pool stands in for the MCP servers.
"""

import asyncio

import pytest

from actions.contracts import ActionKind
from app import audit, cards, messages, orchestrator
from app.answers import CardAnswer
from app.cards import Card, CardState
from app.sessions import Session
from app.tools import ToolName
from core import config, db
from tests.integration.fake_mcp import FakeMcpPool, make_session

ANNA_SUB = config.USERS["anna"].sub

# What the action server returns after changing the tariff, and the reply code
# builds from it.
TARIFF_CHANGED_RESULT = {
    "outcome": "executed",
    "before": "Базовый",
    "after": "Семейный",
    "price_after": 890,
}
TARIFF_CHANGED_REPLY = (
    "Готово: тариф изменён с «Базовый» на «Семейный», 890 ₽/мес."
)
# The audit entry's fields a test compares; `id` and `ts` differ every run.
AUDIT_ENTRY_FIELDS = (
    "who",
    "what",
    "outcome",
    "args",
    "before",
    "after",
    "trace_id",
    "session_id",
    "detail",
)


def create_tariff_card(session: Session) -> Card:
    """Store a pending tariff change card for the session and return it."""
    return cards.create(
        session,
        ActionKind.TARIFF_CHANGE,
        {"tariff": "Семейный"},
        {"Станет": "Семейный, 890 ₽/мес"},
    )


def fetch_card_entries(card: Card) -> list[dict]:
    """Return the audit entries about the card, oldest first."""
    return audit.find_card_entries(orchestrator.build_idempotency_key(card))


def fetch_card_outcomes(card: Card) -> list[tuple[str, str | None]]:
    """Return the outcome and detail of each audit entry about the card."""
    return [
        (entry["outcome"], entry["detail"])
        for entry in fetch_card_entries(card)
    ]


def fetch_card_state(card: Card) -> CardState:
    """Return the state the table holds for the card."""
    stored_card = cards.find_card(card.session_id, card.pending_id)
    assert stored_card is not None
    return stored_card.state


def answer_message(session: Session, message: str) -> str | None:
    """Return code's reply to a message, or None when the agents take it."""
    # A turn starts the same way: it reads the session's newest card, then
    # lets code answer the message before any agent runs.
    latest_card = cards.find_latest_card(session.session_id)
    return asyncio.run(
        orchestrator.apply_message_to_card(session, message, latest_card)
    )


def expire_card(card: Card) -> None:
    """End the card's lifetime a second ago."""
    db.execute(
        "update pending_cards set expires_at = now() - interval '1 second'"
        " where pending_id = %s",
        (card.pending_id,),
    )


def answer_card(
    session: Session, pending_id: str, answer: CardAnswer
) -> str | None:
    """Return the reply to an answer given to a card by its id."""
    # The web chat's card buttons reach this through the decision endpoint.
    return asyncio.run(orchestrator.decide_card(session, pending_id, answer))


def test_yes_executes_the_live_card():
    """The confirmed card goes to the action server with its own key."""
    mcp_pool = FakeMcpPool({ToolName.EXECUTE_ACTION: TARIFF_CHANGED_RESULT})
    session = make_session(mcp_pool)
    card = create_tariff_card(session)

    reply = answer_message(session, "да")

    assert reply == TARIFF_CHANGED_REPLY
    # The key is made of the card's action and id: every "да" to this card
    # gets the same key.
    assert mcp_pool.tool_calls == [
        (
            ToolName.EXECUTE_ACTION,
            {
                "action": "tariff_change",
                "details_json": '{"tariff": "Семейный"}',
                "pending_id": card.pending_id,
                "idempotency_key": f"tariff_change:{card.pending_id}",
            },
        )
    ]
    assert fetch_card_state(card) == "confirmed"
    # The journal has who changed what, from what to what, in which turn.
    [entry] = fetch_card_entries(card)
    assert {field: entry[field] for field in AUDIT_ENTRY_FIELDS} == {
        "who": ANNA_SUB,
        "what": "tariff_change",
        "outcome": "executed",
        "args": {"tariff": "Семейный"},
        "before": "Базовый",
        "after": "Семейный",
        "trace_id": session.trace_id,
        "session_id": session.session_id,
        "detail": None,
    }


def test_repeated_yes_gets_the_duplicate_reply():
    """A second "да" finds the card's key taken and executes nothing."""
    mcp_pool = FakeMcpPool({ToolName.EXECUTE_ACTION: TARIFF_CHANGED_RESULT})
    session = make_session(mcp_pool)
    card = create_tariff_card(session)
    answer_message(session, "да")

    reply = answer_message(session, "да")

    assert reply == messages.ACTION_DUPLICATE
    # Only the first "да" reached the action server.
    assert len(mcp_pool.tool_calls) == 1
    assert fetch_card_outcomes(card) == [
        ("executed", None),
        ("duplicate_suppressed", None),
    ]


def test_billing_duplicate_on_the_first_yes_is_an_execution():
    """Our key was free, so the tariff changed during this "да"."""
    # McpPool retried after a lost response, and billing knew the key.
    mcp_pool = FakeMcpPool(
        {
            ToolName.EXECUTE_ACTION: {
                **TARIFF_CHANGED_RESULT,
                "outcome": "duplicate",
            }
        }
    )
    session = make_session(mcp_pool)
    card = create_tariff_card(session)

    reply = answer_message(session, "да")

    assert reply == TARIFF_CHANGED_REPLY
    [entry] = fetch_card_entries(card)
    assert (
        entry["outcome"],
        entry["before"],
        entry["after"],
        entry["detail"],
    ) == (
        "executed",
        "Базовый",
        "Семейный",
        "billing had the key: the call was retried",
    )


def test_card_button_after_a_typed_yes_gets_the_duplicate_reply():
    """The card's key stops a second confirmation of the same card."""
    mcp_pool = FakeMcpPool({ToolName.EXECUTE_ACTION: TARIFF_CHANGED_RESULT})
    session = make_session(mcp_pool)
    card = create_tariff_card(session)
    answer_message(session, "да")

    # The card's button still works after the typed "да".
    reply = answer_card(session, card.pending_id, CardAnswer.YES)

    assert reply == messages.ACTION_DUPLICATE
    assert len(mcp_pool.tool_calls) == 1


def test_no_to_a_confirmed_card_goes_to_the_agents():
    """Only a repeated "да" refers to a confirmed card."""
    mcp_pool = FakeMcpPool({ToolName.EXECUTE_ACTION: TARIFF_CHANGED_RESULT})
    session = make_session(mcp_pool)
    card = create_tariff_card(session)
    answer_message(session, "да")

    reply = answer_message(session, "нет")

    # None means the message goes to the agents.
    assert reply is None
    assert fetch_card_state(card) == "confirmed"


@pytest.mark.parametrize(
    ("failed_execution", "error_type"),
    [
        # The action server is down.
        (RuntimeError("server is down"), "RuntimeError"),
        # Its answer breaks the contract: it has no outcome.
        ({"status": "done"}, "ValidationError"),
    ],
)
def test_yes_after_a_failed_execution_retries_the_card(
    failed_execution, error_type
):
    """A failure leaves the card pending, so the next "да" runs it again."""
    mcp_pool = FakeMcpPool({ToolName.EXECUTE_ACTION: failed_execution})
    session = make_session(mcp_pool)
    card = create_tariff_card(session)

    failed_reply = answer_message(session, "да")
    state_after_failure = fetch_card_state(card)
    # The action server answers properly again.
    mcp_pool.result_by_tool[ToolName.EXECUTE_ACTION] = TARIFF_CHANGED_RESULT
    retry_reply = answer_message(session, "да")

    assert failed_reply == messages.ACTION_FAILED
    assert state_after_failure == "pending"
    assert retry_reply == TARIFF_CHANGED_REPLY
    assert fetch_card_state(card) == "confirmed"
    # The same key both times, so a retry can never execute the card twice.
    idempotency_keys = [
        arguments["idempotency_key"] for _, arguments in mcp_pool.tool_calls
    ]
    assert idempotency_keys == [f"tariff_change:{card.pending_id}"] * 2
    assert fetch_card_outcomes(card) == [
        ("error", error_type),
        ("executed", None),
    ]


def test_no_rejects_the_live_card():
    """A cancelled card is closed and nothing is executed."""
    mcp_pool = FakeMcpPool({})
    session = make_session(mcp_pool)
    card = create_tariff_card(session)

    reply = answer_message(session, "нет, давай не будем")

    assert reply == messages.CARD_REJECTED
    assert mcp_pool.tool_calls == []
    assert fetch_card_state(card) == "rejected"
    assert fetch_card_outcomes(card) == [("rejected_by_user", None)]


def test_change_of_topic_leaves_the_card_live():
    """A question is not an answer: the agents take it, the card waits."""
    mcp_pool = FakeMcpPool({})
    session = make_session(mcp_pool)
    card = create_tariff_card(session)

    reply = answer_message(session, "а когда спишут деньги?")

    assert reply is None
    assert mcp_pool.tool_calls == []
    assert fetch_card_state(card) == "pending"


def test_yes_to_an_expired_card_executes_nothing():
    """A card past its lifetime takes no answer."""
    mcp_pool = FakeMcpPool({ToolName.EXECUTE_ACTION: TARIFF_CHANGED_RESULT})
    session = make_session(mcp_pool)
    card = create_tariff_card(session)
    expire_card(card)

    reply = answer_message(session, "да")

    assert reply is None
    assert mcp_pool.tool_calls == []


def test_refused_execution_leaves_the_card_pending():
    """After a refusal the card can still be handed to an operator."""
    mcp_pool = FakeMcpPool(
        {
            ToolName.EXECUTE_ACTION: {"outcome": "daily_limit", "limit": 2},
            ToolName.CREATE_TICKET: {"ticket_id": "t_1674"},
        }
    )
    session = make_session(mcp_pool)
    card = create_tariff_card(session)

    refusal_reply = answer_message(session, "да")
    state_after_refusal = fetch_card_state(card)
    # The refusal reply suggests asking for an operator.
    ticket_reply = answer_message(session, "оператор")

    assert refusal_reply == messages.ACTION_DAILY_LIMIT
    assert state_after_refusal == "pending"
    assert ticket_reply == messages.TICKET_CREATED.format(ticket_id="t_1674")
    # The ticket carries the card's rows and what the execution ran into.
    # I18N: the note's attempts line.
    assert mcp_pool.tool_calls[-1][1]["note"] == (
        "Станет: Семейный, 890 ₽/мес\n"
        "Попытки исполнения: server_limit (daily_limit)"
    )
    assert fetch_card_state(card) == "rejected"
    assert fetch_card_outcomes(card) == [
        ("server_limit", "daily_limit"),
        ("escalated", "t_1674"),
    ]


def test_asking_for_an_operator_turns_the_card_into_a_ticket():
    """The ticket carries what the card showed, then the card is closed."""
    mcp_pool = FakeMcpPool({ToolName.CREATE_TICKET: {"ticket_id": "t_1674"}})
    session = make_session(mcp_pool)
    card = create_tariff_card(session)

    reply = answer_message(session, "позови оператора")

    assert reply == messages.TICKET_CREATED.format(ticket_id="t_1674")
    # The note holds the card's rows, so the operator sees what was offered.
    # I18N: the note's attempts line.
    assert mcp_pool.tool_calls == [
        (
            ToolName.CREATE_TICKET,
            {
                "subject": "Смена тарифа",
                "note": (
                    "Станет: Семейный, 890 ₽/мес\nПопыток исполнения не было"
                ),
            },
        )
    ]
    assert fetch_card_state(card) == "rejected"
    assert fetch_card_outcomes(card) == [("escalated", "t_1674")]


def test_failed_ticket_leaves_the_card_live():
    """Without a ticket the card must stay answerable."""
    mcp_pool = FakeMcpPool(
        {ToolName.CREATE_TICKET: RuntimeError("server is down")}
    )
    session = make_session(mcp_pool)
    card = create_tariff_card(session)

    with pytest.raises(RuntimeError):
        answer_message(session, "позови оператора")

    assert fetch_card_state(card) == "pending"
    # The failure reaches the caller and is not recorded.
    assert fetch_card_outcomes(card) == []


def test_ticket_without_an_id_leaves_the_card_live():
    """An answer without a ticket id is no ticket: the subscriber may retry."""
    mcp_pool = FakeMcpPool({ToolName.CREATE_TICKET: {"status": "error"}})
    session = make_session(mcp_pool)
    card = create_tariff_card(session)

    reply = answer_message(session, "позови оператора")

    assert reply == messages.TICKET_FAILED
    assert fetch_card_state(card) == "pending"
    assert fetch_card_outcomes(card) == [("error", "ticket_failed")]


def send_new_card_replies(session: Session, card: Card) -> list[dict]:
    """Return the events a turn sends for the card its agents created."""
    sent_events = []

    async def collect_event(event: dict) -> None:
        sent_events.append(event)

    # No other runs, so the editor isn't called.
    asyncio.run(
        orchestrator.send_card_and_ticket_replies(
            session, "смени тариф", [], card, [], collect_event
        )
    )
    return sent_events


def test_new_live_card_is_shown():
    """The card goes to the browser, then the text that asks to answer it."""
    session = make_session(FakeMcpPool({}))
    card = create_tariff_card(session)

    sent_events = send_new_card_replies(session, card)

    assert [event["type"] for event in sent_events] == ["card", "reply"]
    assert sent_events[1]["text"] == messages.CARD_READY


def test_new_card_that_expired_during_the_turn_is_not_shown():
    """It can't be answered any more, so the turn says so instead."""
    session = make_session(FakeMcpPool({}))
    card = create_tariff_card(session)
    # The agents and the editor took longer than the card's lifetime.
    expire_card(card)

    sent_events = send_new_card_replies(session, card)

    assert [event["type"] for event in sent_events] == ["reply"]
    assert sent_events[0]["text"] == messages.CARD_EXPIRED


def test_answer_by_card_id_is_recorded_in_the_dialogue():
    """An answer given without text is remembered as "да" and its reply."""
    mcp_pool = FakeMcpPool({ToolName.EXECUTE_ACTION: TARIFF_CHANGED_RESULT})
    session = make_session(mcp_pool)
    card = create_tariff_card(session)

    reply = answer_card(session, card.pending_id, CardAnswer.YES)

    assert reply == TARIFF_CHANGED_REPLY
    assert session.planner_messages == [
        {"role": "user", "content": "да"},
        {"role": "assistant", "content": TARIFF_CHANGED_REPLY},
    ]


def test_card_of_another_session_is_not_answered():
    """A card's id alone is not enough: it must be the session's own card."""
    mcp_pool = FakeMcpPool({ToolName.EXECUTE_ACTION: TARIFF_CHANGED_RESULT})
    card = create_tariff_card(make_session(mcp_pool))
    other_session = make_session(mcp_pool)

    reply = answer_card(other_session, card.pending_id, CardAnswer.YES)

    assert reply is None
    assert mcp_pool.tool_calls == []
    assert fetch_card_state(card) == "pending"
