"""The action server's contract, called in process. No services are needed."""

import asyncio
import json

import pytest
from mcp.client.client import Client
from mcp.shared.exceptions import MCPError

from actions import server
from core import config

ANNA_SUB = config.USERS["anna"].sub


async def refusal_of(tool_name: str, arguments: dict) -> dict:
    """Return the error data of a refused call."""
    async with Client(server.actions_server) as client:
        # Caught inside: leaving the client's task group wraps the error.
        try:
            await client.call_tool(tool_name, arguments)
        except MCPError as error:
            return error.data
    raise AssertionError("the call was not refused")


async def call_tool(tool_name: str, arguments: dict) -> dict:
    """Return the result of a successful call."""
    async with Client(server.actions_server) as client:
        result = await client.call_tool(tool_name, arguments)
    # The server sends a dict as JSON text in the first content block.
    return json.loads(result.content[0].text)


@pytest.mark.parametrize(
    "arguments",
    [
        {"sub": ANNA_SUB, "phone": None, "email": None},
        {"sub": ANNA_SUB, "phone": "", "email": " "},
        {"sub": ANNA_SUB},
    ],
)
def test_contacts_without_a_new_value_are_refused(arguments):
    """Null, blank and omitted contacts all mean there is nothing to update."""
    # Models send null for a contact they keep. The tool's schema must let
    # null through, so the refusal comes from the business check with a code
    # the model can act on, and not from argument validation.
    error_data = asyncio.run(refusal_of("prepare_contacts_update", arguments))

    assert error_data["error"] == "nothing_to_update"


def test_null_phone_keeps_the_phone_and_changes_the_email(monkeypatch):
    """A null contact is kept, the other one goes to the card."""

    async def fake_fetch_subscriber(_sub: str) -> dict:
        return {"phone": "+7 900 111-22-33", "email": "old@example.ru"}

    monkeypatch.setattr(
        server.action_service, "fetch_subscriber", fake_fetch_subscriber
    )

    prepared_action = asyncio.run(
        call_tool(
            "prepare_contacts_update",
            {"sub": ANNA_SUB, "phone": None, "email": "New@Example.ru"},
        )
    )

    assert prepared_action["details"] == {"email": "new@example.ru"}
    assert prepared_action["view"] == {
        "Почта": "old@example.ru → new@example.ru"
    }
