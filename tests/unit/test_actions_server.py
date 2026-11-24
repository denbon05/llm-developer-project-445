"""The action server's contract, called in process. No services are needed."""

import asyncio
import json

import pytest
from mcp.client.client import Client
from mcp.server.auth.provider import AccessToken
from mcp.shared.exceptions import MCPError
from mcp.types import TextContent

from actions import server
from core import config

ANNA_SUB = config.USERS["anna"].sub


@pytest.fixture(autouse=True)
def verified_token_of_anna(monkeypatch) -> None:
    """Make every call carry anna's verified token."""
    # An in-process client skips the HTTP layer, where the SDK verifies the
    # Bearer token and stores it for the tools.
    access_token = AccessToken(
        token="test-token", client_id="test", scopes=[], subject=ANNA_SUB
    )
    monkeypatch.setattr(server, "get_access_token", lambda: access_token)


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
    # The server sends a dict as JSON text in the first content block. A block
    # may also be an image, audio or a resource, which have no text.
    first_block = result.content[0]
    assert isinstance(first_block, TextContent)
    return json.loads(first_block.text)


def test_tools_act_for_the_tokens_subject(monkeypatch):
    """No argument names the subscriber: the service gets the token's one."""
    ticket_subjects = []

    async def stub_create_ticket(sub: str, _subject: str, _note: str) -> dict:
        ticket_subjects.append(sub)
        return {"ticket_id": "t_1674"}

    monkeypatch.setattr(
        server.action_service, "create_ticket", stub_create_ticket
    )

    asyncio.run(call_tool("create_ticket", {"subject": "s", "note": "n"}))

    assert ticket_subjects == [ANNA_SUB]


@pytest.mark.parametrize(
    "arguments",
    [
        {"phone": None, "email": None},
        {"phone": "", "email": " "},
        {},
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

    async def stub_fetch_subscriber(_sub: str) -> dict:
        return {"phone": "+7 900 111-22-33", "email": "old@example.ru"}

    monkeypatch.setattr(
        server.action_service, "fetch_subscriber", stub_fetch_subscriber
    )

    prepared_action = asyncio.run(
        call_tool(
            "prepare_contacts_update",
            {"phone": None, "email": "New@Example.ru"},
        )
    )

    assert prepared_action["details"] == {"email": "new@example.ru"}
    assert prepared_action["view"] == {
        "Почта": "old@example.ru → new@example.ru"
    }
