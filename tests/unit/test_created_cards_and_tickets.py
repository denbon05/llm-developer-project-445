"""Finding the card or ticket a worker run created. No services needed."""

import json

import pytest

from app.cards import Card
from app.llm import AgentRun, ToolCall
from app.orchestrator import (
    find_new_card,
    find_ticket_id,
    has_created_card,
    has_created_card_or_ticket,
)
from app.tools import ToolName
from core import config

ANNA_SUB = config.USERS["anna"].sub


def run_with(*tool_outputs: tuple[str, dict | str]) -> AgentRun:
    """Return a run whose tool calls returned the given outputs."""
    tool_calls = [
        ToolCall(
            tool_name, output if isinstance(output, str) else json.dumps(output)
        )
        for tool_name, output in tool_outputs
    ]
    return AgentRun("report", tool_calls, 1.0)


def test_ticket_id_comes_from_the_tool_output():
    """The id is copied by code, so the model can't distort it."""
    run = run_with(
        (ToolName.CREATE_TICKET, {"ticket_id": "t_1674", "status": "open"})
    )

    assert find_ticket_id(run) == "t_1674"
    assert has_created_card_or_ticket(run)


def test_failed_ticket_call_created_no_ticket():
    """A ticket exists only when the tool output carries its id."""
    run = run_with((ToolName.CREATE_TICKET, {"error": "tool_failed"}))

    assert find_ticket_id(run) is None
    assert not has_created_card_or_ticket(run)


def test_output_that_is_not_json_is_no_ticket():
    """A raw tool error must not break the turn."""
    run = run_with((ToolName.CREATE_TICKET, "Error executing tool"))

    assert find_ticket_id(run) is None


def test_card_is_found_among_other_tool_calls():
    """One prepare call that created a card is enough."""
    run = run_with(
        (ToolName.GET_MY_TARIFF, {"tariff": "Базовый"}),
        (ToolName.PREPARE_TARIFF_CHANGE, {"card_created": True}),
    )

    assert has_created_card(run)
    assert has_created_card_or_ticket(run)


def test_refused_prepare_created_no_card():
    """A refusal from a prepare tool is not a card."""
    run = run_with((ToolName.PREPARE_TARIFF_CHANGE, {"error": "same_tariff"}))

    assert not has_created_card_or_ticket(run)


def make_card(pending_id: str) -> Card:
    """Return a tariff change card with this id."""
    return Card(pending_id, "tariff_change", {}, {}, "sess_1", ANNA_SUB)


# After the agents, the turn must decide whether to show a card: only one the
# agents created in this turn. It reads the session's newest card before the
# agents and again after them; if it changed, the agents created it. Each row
# is one turn of a dialogue. Card ids start with "p_" (pending) like real
# ones; "old" and "new" say which card is which.
@pytest.mark.parametrize(
    ("newest_card_id_before", "newest_card_id_after", "new_card_id"),
    [
        # «переведи на семейный»: the agents prepare the session's first
        # card, so it is shown.
        (None, "p_new", "p_new"),
        # «а когда спишут деньги?»: the agents only answer the question. The
        # waiting card is still the newest, so it isn't shown again.
        ("p_old", "p_old", None),
        # «а давай лучше эконом»: the agents prepare a card that replaces the
        # waiting one, so the new card is shown.
        ("p_old", "p_new", "p_new"),
    ],
)
def test_finds_the_card_the_agents_created_in_this_turn(
    newest_card_id_before, newest_card_id_after, new_card_id
):
    """The newest card before and after the agents tells what they created."""
    newest_card_before = (
        make_card(newest_card_id_before) if newest_card_id_before else None
    )
    newest_card_after = make_card(newest_card_id_after)

    new_card = find_new_card(newest_card_before, newest_card_after)

    assert (new_card.pending_id if new_card else None) == new_card_id
