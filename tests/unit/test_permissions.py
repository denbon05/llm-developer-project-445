"""What a session's token lets its models call. No services are needed."""

import pytest

from app import permissions
from app.permissions import Decision
from app.tools import ToolName

ANNA_CLAIMS = {
    "sub": "u_4f2a",
    "role": "client",
    "scope": "tariffs:read tariffs:write charges:read",
}
BORIS_CLAIMS = {
    "sub": "u_9b1c",
    "role": "client",
    "scope": "tariffs:read charges:read",
}
OLGA_CLAIMS = {
    "sub": "u_op7",
    "role": "operator",
    "scope": "tariffs:read charges:read support:read",
}


# The expected names are listed by hand: built from the groups, the test
# would only repeat them.
@pytest.mark.parametrize(
    ("claims", "expected_tool_names"),
    [
        # A client who may change the tariff: also the three cards, never
        # execute_action.
        (
            ANNA_CLAIMS,
            [
                "get_my_profile",
                "get_my_tariff",
                "list_tariffs",
                "list_my_charges",
                "search_kb",
                "prepare_tariff_change",
                "prepare_toggle_service",
                "prepare_contacts_update",
                "create_ticket",
            ],
        ),
        # A client without tariffs:write: no cards.
        (
            BORIS_CLAIMS,
            [
                "get_my_profile",
                "get_my_tariff",
                "list_tariffs",
                "list_my_charges",
                "search_kb",
                "create_ticket",
            ],
        ),
        # An operator: another subscriber's overview, no data of their own.
        (
            OLGA_CLAIMS,
            [
                "list_tariffs",
                "get_subscriber_overview",
                "search_kb",
                "create_ticket",
            ],
        ),
        # A role the allowlist doesn't know sees nothing, whatever its scope.
        ({"sub": "u_x", "role": "auditor", "scope": "tariffs:write"}, []),
    ],
)
def test_claims_decide_the_visible_tools(claims, expected_tool_names):
    """Barrier 1 keeps the allowed names, in the order given."""
    allowlist = permissions.allowlist_from_claims(claims)

    assert (
        permissions.visible_tools(allowlist, list(ToolName))
        == expected_tool_names
    )


@pytest.mark.parametrize(
    ("claims", "operation", "args", "expected_decision"),
    [
        # An operation of the session's own, with ordinary arguments.
        (
            ANNA_CLAIMS,
            ToolName.PREPARE_TARIFF_CHANGE,
            {"tariff": "семейный"},
            Decision(allowed=True),
        ),
        # Outside the allowlist: boris may not prepare a card.
        (
            BORIS_CLAIMS,
            ToolName.PREPARE_TARIFF_CHANGE,
            {"tariff": "семейный"},
            Decision(
                allowed=False,
                reason="operation_not_allowed:prepare_tariff_change",
            ),
        ),
        # A subscriber named in the arguments: the subject comes from the
        # token only.
        (
            ANNA_CLAIMS,
            ToolName.PREPARE_TARIFF_CHANGE,
            {"sub": "u_9b1c", "tariff": "семейный"},
            Decision(allowed=False, reason="object_from_arguments:sub"),
        ),
        # A new phone is a contact value, not an identity.
        (
            ANNA_CLAIMS,
            ToolName.PREPARE_CONTACTS_UPDATE,
            {"phone": "+7 900 111-22-33", "email": None},
            Decision(allowed=True),
        ),
        # An operator names the subscriber to look at; the CRM server checks
        # the role.
        (
            OLGA_CLAIMS,
            ToolName.GET_SUBSCRIBER_OVERVIEW,
            {"subscriber_id": "u_4f2a"},
            Decision(allowed=True),
        ),
        # Only code executes a confirmed card.
        (
            ANNA_CLAIMS,
            ToolName.EXECUTE_ACTION,
            {},
            Decision(
                allowed=False, reason="operation_not_allowed:execute_action"
            ),
        ),
    ],
)
def test_admit_checks_the_operation_and_its_arguments(
    claims, operation, args, expected_decision
):
    """Barrier 2 refuses a hidden operation and a subscriber in the arguments."""
    allowlist = permissions.allowlist_from_claims(claims)

    assert permissions.admit(allowlist, operation, args) == expected_decision
