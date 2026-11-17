"""The tool wrapper hides the subject and turns prepare results into cards.

A fake MCP pool stands in for the MCP servers, so no services are needed.
"""

import asyncio
import json

from app import wrap
from app.cards import Card
from app.sessions import Session
from app.tools import ToolName
from core import config

ANNA_SUB = config.USERS["anna"].sub
BORIS_SUB = config.USERS["boris"].sub

TICKET_SCHEMA = {
    "type": "object",
    "properties": {
        "sub": {"type": "string"},
        "subject": {"type": "string"},
        "note": {"type": "string"},
    },
    "required": ["sub", "subject", "note"],
}
PREPARE_SCHEMA = {
    "type": "object",
    "properties": {"sub": {"type": "string"}, "tariff": {"type": "string"}},
    "required": ["sub", "tariff"],
}
READ_SCHEMA = {"type": "object", "properties": {}}
PREPARED = {
    "ok": True,
    "action": "tariff_change",
    "details": {"tariff": "Семейный"},
    "view": {"Станет": "Семейный, 890 ₽/мес"},
}


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


def make_session(mcp_pool: FakeMcpPool) -> Session:
    """Return anna's session backed by the fake MCP pool."""
    return Session(
        login="anna",
        user=config.USERS["anna"],
        tokens=None,  # pyright: ignore[reportArgumentType]
        mcp=mcp_pool,  # pyright: ignore[reportArgumentType]
    )


def invoke(tool, arguments: dict) -> dict:
    """Run the wrapped tool the way the agent SDK does."""
    output = asyncio.run(tool.on_invoke_tool(None, json.dumps(arguments)))
    return json.loads(output)


def test_model_never_sees_the_subject():
    """The schema given to the model has no `sub`, the rest is unchanged."""
    mcp_pool = FakeMcpPool({ToolName.CREATE_TICKET: TICKET_SCHEMA}, {})

    tool = wrap.wrap_mcp_tool(make_session(mcp_pool), ToolName.CREATE_TICKET)

    assert set(tool.params_json_schema["properties"]) == {"subject", "note"}
    assert tool.params_json_schema["required"] == ["subject", "note"]
    assert "sub" in TICKET_SCHEMA["properties"]  # the MCP pool's copy is intact


def test_subject_is_always_the_sessions_subscriber():
    """Whatever the model sends as `sub` is overwritten."""
    mcp_pool = FakeMcpPool({ToolName.CREATE_TICKET: TICKET_SCHEMA}, {})
    tool = wrap.wrap_mcp_tool(make_session(mcp_pool), ToolName.CREATE_TICKET)

    # The session is anna's, but the model sends boris's id.
    invoke(tool, {"sub": BORIS_SUB, "subject": "s", "note": "n"})

    # The MCP server received anna's id: the wrapper replaced the model's.
    assert mcp_pool.call_arguments == [
        {"sub": ANNA_SUB, "subject": "s", "note": "n"}
    ]


def test_tools_without_a_subject_are_left_alone():
    """Read tools take the subject from the token and get no `sub`."""
    mcp_pool = FakeMcpPool({ToolName.GET_MY_TARIFF: READ_SCHEMA}, {})
    tool = wrap.wrap_mcp_tool(make_session(mcp_pool), ToolName.GET_MY_TARIFF)

    invoke(tool, {})

    assert tool.params_json_schema["properties"] == {}
    assert mcp_pool.call_arguments == [{}]


def test_successful_prepare_creates_a_card(monkeypatch):
    """The model gets a short notice, the card goes to the session."""
    created_cards = []

    def fake_create(session, action, details, view):
        card = Card(
            "p_1", action, details, view, session.session_id, session.user.sub
        )
        created_cards.append(card)
        return card

    monkeypatch.setattr(wrap.cards, "create", fake_create)
    mcp_pool = FakeMcpPool(
        {ToolName.PREPARE_TARIFF_CHANGE: PREPARE_SCHEMA},
        {ToolName.PREPARE_TARIFF_CHANGE: PREPARED},
    )
    tool = wrap.wrap_mcp_tool(
        make_session(mcp_pool), ToolName.PREPARE_TARIFF_CHANGE
    )

    tool_output = invoke(tool, {"tariff": "семейный"})

    assert tool_output["card_created"] is True
    assert tool_output["card"] == PREPARED["view"]
    assert [card.details for card in created_cards] == [{"tariff": "Семейный"}]
