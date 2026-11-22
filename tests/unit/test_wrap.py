"""The tool wrapper checks the allowlist and turns prepare results into cards.

Fakes stand in for the MCP servers and the PDP, so no services are needed.
"""

import asyncio
import json

import pytest
from agents import FunctionToolResult, ToolsToFinalOutputResult

from app import messages, wrap
from app.cards import Card
from app.pep import Pep
from app.permissions import Allowlist, Decision
from app.sessions import Session
from app.tools import ToolName
from core import config

BORIS_SUB = config.USERS["boris"].sub

PREPARE_SCHEMA = {
    "type": "object",
    "properties": {"tariff": {"type": "string"}},
    "required": ["tariff"],
}
READ_SCHEMA = {"type": "object", "properties": {}}
PREPARED = {
    "ok": True,
    "action": "tariff_change",
    "details": {"tariff": "Семейный"},
    "view": {"Станет": "Семейный, 890 ₽/мес"},
}
# What anna's models may call of the tools these tests use.
ANNA_ALLOWLIST: Allowlist = frozenset(
    {ToolName.PREPARE_TARIFF_CHANGE, ToolName.GET_MY_TARIFF}
)


class FakeMcpPool:
    """Answers like an MCP pool and remembers the arguments of each call."""

    def __init__(self, schema_by_tool: dict, result_by_tool: dict) -> None:
        self.schema_by_tool = schema_by_tool
        self.result_by_tool = result_by_tool
        self.call_arguments: list[dict] = []

    def tool_names(self) -> list[str]:
        return sorted(self.schema_by_tool)

    def tool_schema(self, name: str) -> dict:
        return self.schema_by_tool[name]

    def tool_description(self, name: str) -> str:
        return name

    def resource_of(self, _name: str) -> str:
        return "actions"

    async def call(
        self, _resource_key: str, name: str, arguments: dict
    ) -> dict:
        self.call_arguments.append(arguments)
        return self.result_by_tool.get(name, {})


class FakePep(Pep):
    """Gives every stage of a card the same decision, without a PDP."""

    def __init__(self, decision: Decision) -> None:
        # No PDP session and no tokens: nothing is sent.
        super().__init__(session_id="", tokens=None)  # pyright: ignore[reportArgumentType]
        self.decision = decision

    async def request_decision(self, action: str, card: Card) -> Decision:
        return self.decision


def make_session(
    mcp_pool: FakeMcpPool, allowlist: Allowlist = ANNA_ALLOWLIST
) -> Session:
    """Return anna's session backed by the fake MCP pool."""
    return Session(
        login="anna",
        user=config.USERS["anna"],
        tokens=None,  # pyright: ignore[reportArgumentType]
        mcp=mcp_pool,  # pyright: ignore[reportArgumentType]
        allowlist=allowlist,
    )


def invoke(tool, arguments: dict) -> dict:
    """Run the wrapped tool the way the agent SDK does."""
    output = asyncio.run(tool.on_invoke_tool(None, json.dumps(arguments)))
    return json.loads(output)


def test_agent_gets_only_offered_and_visible_tools():
    """A tool the allowlist hides or no server offers is left out."""
    mcp_pool = FakeMcpPool(
        {
            ToolName.PREPARE_TARIFF_CHANGE: PREPARE_SCHEMA,
            ToolName.GET_MY_TARIFF: READ_SCHEMA,
        },
        {},
    )
    # A session that may read but not prepare cards, like boris's.
    session = make_session(
        mcp_pool,
        allowlist=frozenset({ToolName.GET_MY_TARIFF, ToolName.GET_MY_PROFILE}),
    )

    agent_tools = wrap.build_agent_tools(
        session,
        [
            ToolName.PREPARE_TARIFF_CHANGE,
            ToolName.GET_MY_TARIFF,
            # Allowed, but no server of the session offers it.
            ToolName.GET_MY_PROFILE,
        ],
    )

    assert [tool.name for tool in agent_tools] == [ToolName.GET_MY_TARIFF]


def test_denied_call_never_reaches_the_server():
    """A call naming a subscriber gets the denial instead of an answer."""
    mcp_pool = FakeMcpPool(
        {ToolName.PREPARE_TARIFF_CHANGE: PREPARE_SCHEMA},
        {ToolName.PREPARE_TARIFF_CHANGE: PREPARED},
    )
    session = make_session(mcp_pool)
    tool = wrap.build_agent_tool(session, ToolName.PREPARE_TARIFF_CHANGE)

    # anna's session, but the model names boris.
    tool_output = invoke(tool, {"sub": BORIS_SUB, "tariff": "семейный"})

    assert tool_output == {
        "error": "denied",
        "reason": "object_from_arguments:sub",
        "retryable": False,
    }
    assert mcp_pool.call_arguments == []
    assert session.used_tools == []


@pytest.mark.parametrize(
    ("prepare_decision", "expected_stored_details", "expected_run_end"),
    [
        # Allowed: the card is stored, and the model reads the notice.
        (
            Decision(allowed=True),
            [{"tariff": "Семейный"}],
            ToolsToFinalOutputResult(is_final_output=False),
        ),
        # No decision from the PDP: no card, and the run ends with the text
        # for that reason.
        (
            Decision(allowed=False, reason="pdp_unavailable"),
            [],
            ToolsToFinalOutputResult(
                is_final_output=True, final_output=messages.CHANGE_UNAVAILABLE
            ),
        ),
    ],
)
def test_prepare_stores_a_card_the_policy_allows(
    monkeypatch, prepare_decision, expected_stored_details, expected_run_end
):
    """A card the policy refused never exists."""
    stored_cards: list[Card] = []
    monkeypatch.setattr(
        wrap.cards,
        "store_card",
        lambda _session, card: stored_cards.append(card),
    )
    mcp_pool = FakeMcpPool(
        {ToolName.PREPARE_TARIFF_CHANGE: PREPARE_SCHEMA},
        {ToolName.PREPARE_TARIFF_CHANGE: PREPARED},
    )
    session = make_session(mcp_pool)
    session.pep = FakePep(prepare_decision)
    tool = wrap.build_agent_tool(session, ToolName.PREPARE_TARIFF_CHANGE)

    tool_output = invoke(tool, {"tariff": "семейный"})

    assert [card.details for card in stored_cards] == expected_stored_details
    # The SDK passes the step's outputs to the run's stop check.
    tool_result = FunctionToolResult(
        tool=None,  # pyright: ignore[reportArgumentType]
        output=json.dumps(tool_output),
        run_item=None,  # pyright: ignore[reportArgumentType]
    )
    assert (
        wrap.stop_run_at_denial(None, [tool_result])  # pyright: ignore[reportArgumentType]
        == expected_run_end
    )


@pytest.mark.parametrize(
    ("tool_outputs", "expected_result"),
    [
        # A denial among the step's outputs ends the run with the fixed
        # report.
        (
            [
                json.dumps({"tariff": "Базовый"}),
                json.dumps(
                    {
                        "error": "denied",
                        "reason": "object_from_arguments:sub",
                        "retryable": False,
                    }
                ),
            ],
            ToolsToFinalOutputResult(
                is_final_output=True, final_output=messages.OPERATION_DENIED
            ),
        ),
        # Without one the model reads the outputs, a server's refusal too.
        (
            [json.dumps({"error": "unknown_tariff", "retryable": True})],
            ToolsToFinalOutputResult(is_final_output=False),
        ),
    ],
)
def test_denial_ends_the_agent_run(tool_outputs, expected_result):
    """The model never sees a denial, so it can't retry around it."""
    tool_results = [
        # The check reads only the output: no tool or run item is needed.
        FunctionToolResult(tool=None, output=tool_output, run_item=None)  # pyright: ignore[reportArgumentType]
        for tool_output in tool_outputs
    ]

    # The check reads no run context either.
    assert (
        wrap.stop_run_at_denial(None, tool_results)  # pyright: ignore[reportArgumentType]
        == expected_result
    )
