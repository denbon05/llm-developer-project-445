"""What the tool wrapper records about a model's tool calls, on the real tables.

A stub MCP pool stands in for the MCP servers.
"""

import asyncio
import json

import pytest
from mcp.shared.exceptions import MCPError

from actions.contracts import ActionKind, McpErrorCode
from app import audit, wrap
from app.audit import AuditOutcome
from app.sessions import Session
from app.tools import ToolName
from core import config
from tests.integration.stub_mcp import StubMcpPool, make_session

ANNA_SUB = config.USERS["anna"].sub
BORIS_SUB = config.USERS["boris"].sub

PREPARED_ACTION = {
    "ok": True,
    "action": "tariff_change",
    "details": {"tariff": "Семейный"},
    "view": {"Станет": "Семейный, 890 ₽/мес"},
}
# How the MCP client raises the action server's refusal.
UNKNOWN_TARIFF_REFUSAL = MCPError(
    code=McpErrorCode.ACTION_REFUSED,
    message="unknown_tariff",
    data={"error": "unknown_tariff", "retryable": True, "available": []},
)
# Longer than telemetry keeps of a string argument.
LONG_NOTE = "н" * 150


def call_wrapped_tool(
    session: Session, tool_name: str, input_json: str
) -> None:
    """Call the tool through its wrapper, the way the agent SDK does."""
    tool = wrap.build_agent_tool(session, tool_name)
    # The wrapper never reads the run context, so the test passes none.
    asyncio.run(tool.on_invoke_tool(None, input_json))  # pyright: ignore[reportArgumentType]


def fetch_recorded_rows(
    session: Session, table: str, fields: tuple[str, ...]
) -> list[dict]:
    """Return the chosen fields of the rows the session's turn left in a table."""
    # Each test's session has a trace of its own, so only its rows come back.
    return [
        {field: item[field] for field in fields}
        for item in audit.trace(session.trace_id)
        if item["source"] == table
    ]


@pytest.mark.parametrize(
    ("tool_name", "input_json", "tool_result", "expected_row"),
    [
        # A result: the arguments as the model sent them, strings cut.
        (
            ToolName.CREATE_TICKET,
            json.dumps({"subject": "Тариф", "note": LONG_NOTE}),
            {"ticket_id": "t_1674"},
            {
                "outcome": "ok",
                "args": {"subject": "Тариф", "note": LONG_NOTE[:100]},
            },
        ),
        # A prepared action became a card.
        (
            ToolName.PREPARE_TARIFF_CHANGE,
            '{"tariff": "семейный"}',
            PREPARED_ACTION,
            {"outcome": "card_created", "args": {"tariff": "семейный"}},
        ),
        # The action server refused the call: its code.
        (
            ToolName.PREPARE_TARIFF_CHANGE,
            '{"tariff": "люкс"}',
            UNKNOWN_TARIFF_REFUSAL,
            {"outcome": "unknown_tariff", "args": {"tariff": "люкс"}},
        ),
        # The server is down.
        (
            ToolName.CREATE_TICKET,
            '{"subject": "Тариф", "note": "оператор"}',
            RuntimeError("server is down"),
            {
                "outcome": "tool_failed",
                "args": {"subject": "Тариф", "note": "оператор"},
            },
        ),
        # Arguments that aren't JSON: the start of the text the model wrote.
        (
            ToolName.CREATE_TICKET,
            "{" + LONG_NOTE,
            {},
            {
                "outcome": "bad_arguments",
                "args": {"raw": ("{" + LONG_NOTE)[:100]},
            },
        ),
        # JSON, but an array instead of an object: the same as above.
        (
            ToolName.PREPARE_TARIFF_CHANGE,
            '[{"tariff": "семейный"}]',
            PREPARED_ACTION,
            {
                "outcome": "bad_arguments",
                "args": {"raw": '[{"tariff": "семейный"}]'},
            },
        ),
    ],
)
def test_every_model_call_leaves_a_tool_call_row(
    tool_name, input_json, tool_result, expected_row
):
    """One row per call, whatever happened, with its outcome and arguments."""
    session = make_session(StubMcpPool({tool_name: tool_result}))

    call_wrapped_tool(session, tool_name, input_json)

    tool_call_rows = fetch_recorded_rows(
        session, "tool_calls", ("tool", "args", "outcome")
    )
    assert tool_call_rows == [{"tool": tool_name, **expected_row}]


@pytest.mark.parametrize(
    ("tool_name", "input_json", "tool_result", "expected_entry"),
    [
        # A refused prepare call: the action it prepares and the code.
        (
            ToolName.PREPARE_TARIFF_CHANGE,
            '{"tariff": "люкс"}',
            UNKNOWN_TARIFF_REFUSAL,
            {
                "what": ActionKind.TARIFF_CHANGE,
                "outcome": AuditOutcome.REJECTED_BY_CONTRACT,
                "args": {"tariff": "люкс"},
                "detail": "unknown_tariff",
            },
        ),
        # A call naming a subscriber is denied before the server: the action
        # it attempts and the reason. The identity key stays, the note goes.
        (
            ToolName.PREPARE_TARIFF_CHANGE,
            json.dumps(
                {"sub": BORIS_SUB, "tariff": "семейный", "note": "для Бориса"}
            ),
            PREPARED_ACTION,
            {
                "what": ActionKind.TARIFF_CHANGE,
                "outcome": AuditOutcome.DENIED,
                "args": {"sub": BORIS_SUB, "tariff": "семейный"},
                "detail": "object_from_arguments:sub",
            },
        ),
        # An agent's ticket: the subject only, the note retells the subscriber.
        (
            ToolName.CREATE_TICKET,
            '{"subject": "Тариф", "note": "хочу поговорить с человеком"}',
            {"ticket_id": "t_1674"},
            {
                "what": ToolName.CREATE_TICKET,
                "outcome": AuditOutcome.ESCALATED,
                "args": {"subject": "Тариф"},
                "detail": "t_1674",
            },
        ),
    ],
)
def test_refused_call_and_ticket_leave_an_audit_entry(
    tool_name, input_json, tool_result, expected_entry
):
    """The journal has the subscriber's attempts, not only executed changes."""
    session = make_session(StubMcpPool({tool_name: tool_result}))

    call_wrapped_tool(session, tool_name, input_json)

    audit_entries = fetch_recorded_rows(
        session, "audit_log", ("who", "what", "outcome", "args", "detail")
    )
    assert audit_entries == [{"who": ANNA_SUB, **expected_entry}]
