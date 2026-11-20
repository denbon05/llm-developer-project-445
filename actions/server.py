"""MCP server for changes to a subscriber's account.

Its tools prepare a confirmation card, execute a confirmed action and create
a support ticket.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Annotated

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.shared.exceptions import MCPError
from pydantic import Field
from starlette.applications import Starlette
from starlette.routing import Mount

from actions import service as action_service
from actions.contracts import (
    ActionError,
    ExecutionResult,
    McpErrorCode,
    PreparedAction,
    TicketResult,
)
from core.config import RESOURCES
from core.server_auth import make_mcp_server

# Every request needs a Bearer token issued for this server: the SDK checks
# its signature, issuer, audience and expiry, and answers 401 otherwise. No
# role or scope is checked here: the backend's allowlist decides who may
# prepare a card, and the business rules still answer every caller.
actions_server = make_mcp_server(
    "actions",
    resource=RESOURCES["actions"],
    instructions=(
        "Changes to a subscriber's tariff, services and contacts. prepare_* "
        "tools only prepare a confirmation card; nothing changes until the "
        "subscriber confirms it."
    ),
)


def read_token_subject() -> str:
    """Return the subscriber named by the call's verified token."""
    access_token = get_access_token()
    # The SDK's auth middleware turns away a request without a valid token,
    # so this fails only if a tool runs outside it.
    if access_token is None or not access_token.subject:
        raise RuntimeError("the call has no verified token")
    return access_token.subject


@contextmanager
def convert_refusals_to_mcp_errors() -> Iterator[None]:
    """Turn an action refusal into an MCP error that carries its data."""
    try:
        yield
    except ActionError as error:
        # `data` travels to the client intact, so the model gets the code and
        # hints such as `available` or `example`.
        raise MCPError(
            code=McpErrorCode.ACTION_REFUSED,
            message=error.code,
            data=error.to_mcp_data(),
        ) from error


@actions_server.tool(
    description=(
        "Prepare a confirmation card for switching the subscriber to another "
        "tariff. Changes nothing: the subscriber must confirm the card. Pass "
        "the tariff name as the subscriber said it; on unknown_tariff the "
        "error lists the available tariffs."
    )
)
async def prepare_tariff_change(
    tariff: Annotated[str, Field(description="Tariff name, e.g. 'Семейный'.")],
) -> PreparedAction:
    """Prepare a tariff change card."""
    with convert_refusals_to_mcp_errors():
        return await action_service.prepare_tariff_change(
            read_token_subject(), tariff
        )


@actions_server.tool(
    description=(
        "Prepare a confirmation card for turning one of the subscriber's "
        "services on or off. Changes nothing: the subscriber must confirm the "
        "card. On unknown_service the error lists the subscriber's services."
    )
)
async def prepare_toggle_service(
    service: Annotated[str, Field(description="Service name, e.g. 'Музыка'.")],
    enabled: Annotated[
        bool, Field(description="true to turn on, false to turn off.")
    ],
) -> PreparedAction:
    """Prepare a service toggle card."""
    with convert_refusals_to_mcp_errors():
        return await action_service.prepare_toggle_service(
            read_token_subject(), service, enabled
        )


@actions_server.tool(
    description=(
        "Prepare a confirmation card for new contact details. Changes "
        "nothing: the subscriber must confirm the card. Pass at least one of "
        "phone and email; omit the other or pass null."
    )
)
async def prepare_contacts_update(
    # Optional and nullable: models send null for a field they don't change.
    phone: Annotated[
        str | None,
        Field(
            description=(
                "New phone, e.g. '+7 900 111-22-33'. Omit or null to keep "
                "the current value."
            )
        ),
    ] = None,
    email: Annotated[
        str | None,
        Field(
            description=(
                "New email address. Omit or null to keep the current value."
            )
        ),
    ] = None,
) -> PreparedAction:
    """Prepare a contacts update card."""
    with convert_refusals_to_mcp_errors():
        return await action_service.prepare_contacts_update(
            read_token_subject(), phone, email
        )


@actions_server.tool(
    description=(
        "Execute an action the subscriber confirmed on a card. Called by "
        "backend code only, never by a model."
    )
)
async def execute_action(
    action: Annotated[
        str,
        Field(description="tariff_change, service_toggle or contacts_update."),
    ],
    details_json: Annotated[
        str, Field(description="The card's details as a JSON object.")
    ],
    pending_id: Annotated[str, Field(description="The confirmed card.")],
    idempotency_key: Annotated[
        str, Field(description="The same key never executes twice.")
    ],
) -> ExecutionResult:
    """Execute a confirmed action."""
    with convert_refusals_to_mcp_errors():
        result = await action_service.execute_action(
            read_token_subject(), action, details_json, idempotency_key
        )
    # The card id goes back with the outcome, so the result names its card.
    result["pending_id"] = pending_id
    return result


@actions_server.tool(
    description=(
        "Create a support ticket so a human operator contacts the subscriber. "
        "Returns ticket_id."
    )
)
async def create_ticket(
    subject: Annotated[str, Field(description="Short topic of the request.")],
    note: Annotated[str, Field(description="What the subscriber needs.")],
) -> TicketResult:
    """Create a support ticket."""
    with convert_refusals_to_mcp_errors():
        return await action_service.create_ticket(
            read_token_subject(), subject, note
        )


@asynccontextmanager
async def lifespan(_app: Starlette) -> AsyncGenerator[None]:
    """Keep the MCP session manager running while the app is up."""
    # The session manager must run in uvicorn's event loop.
    async with actions_server.session_manager.run():
        yield


# Served at {ACTIONS_URL}/actions/mcp, the URL the backend's pool connects to.
app = Starlette(
    routes=[Mount("/actions", app=actions_server.streamable_http_app())],
    lifespan=lifespan,
)
