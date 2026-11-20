"""Session permissions: an allowlist from the token's claims and its checks."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from app.tools import PREPARE_TOOL_NAMES, ToolName

# The names of the operations (tools) a session may call.
Allowlist = frozenset[str]

# Each reads only the caller's own data: the server takes the subject from the
# token.
OWN_DATA_OPS: Allowlist = frozenset(
    {ToolName.GET_MY_PROFILE, ToolName.GET_MY_TARIFF, ToolName.LIST_MY_CHARGES}
)
SHARED_READ_OPS: Allowlist = frozenset(
    {ToolName.LIST_TARIFFS, ToolName.SEARCH_KB}
)
# Each prepares a confirmation card for the token's subject.
CARD_OPS: Allowlist = PREPARE_TOOL_NAMES
# Names another subscriber by id; the CRM server checks the role as well.
OPERATOR_OPS: Allowlist = frozenset({ToolName.GET_SUBSCRIBER_OVERVIEW})
# Every role may create a support ticket.
ESCALATION_OPS: Allowlist = frozenset({ToolName.CREATE_TICKET})
# execute_action is in no group: only code calls it, after a confirmed card.

CLIENT_ROLE = "client"
OPERATOR_ROLE = "operator"
OPS_BY_ROLE: dict[str, Allowlist] = {
    CLIENT_ROLE: OWN_DATA_OPS | SHARED_READ_OPS | ESCALATION_OPS,
    OPERATOR_ROLE: OPERATOR_OPS | SHARED_READ_OPS | ESCALATION_OPS,
}
# The only scope that narrows a role's set: it opens the cards to a client.
WRITE_SCOPE = "tariffs:write"

# Argument keys that name a subscriber. The servers take the subject from the
# token, so a model that sends one tries to act on an account it was told
# about. `phone` is not here: it is a new contact value.
IDENTITY_ARGS = frozenset({"user_id", "account", "subscriber", "msisdn", "sub"})


@dataclass(frozen=True)
class Decision:
    """Whether a tool call may go ahead and, if not, why."""

    allowed: bool
    reason: str = ""


# POST-QA: rename to build_allowlist. tests/smoke.py calls this name.
def allowlist_from_claims(claims: Mapping[str, Any]) -> Allowlist:
    """Return the operations the token's role and scope allow."""
    role = claims.get("role", "")
    # An unknown role may call nothing.
    role_ops = OPS_BY_ROLE.get(role, frozenset())
    # `scope` is one space-separated string, as OAuth sends it.
    scopes = str(claims.get("scope", "")).split()
    if role == CLIENT_ROLE and WRITE_SCOPE in scopes:
        return role_ops | CARD_OPS
    return role_ops


# POST-QA: rename to filter_visible_tools. tests/smoke.py calls this name.
def visible_tools(allowlist: Allowlist, tool_names: Iterable[str]) -> list[str]:
    """Return the tool names in the allowlist, in their order."""
    return [tool_name for tool_name in tool_names if tool_name in allowlist]


def has_cross_account_access(visible_tool_names: frozenset[str]) -> bool:
    """Tell whether a session's models may reach other subscribers' accounts."""
    # Any operator operation counts, so a new one joins without a change here.
    return bool(OPERATOR_OPS & visible_tool_names)


def admit(
    allowlist: Allowlist, operation: str, args: Mapping[str, Any]
) -> Decision:
    """Decide whether a model's tool call may reach its server."""
    if operation not in allowlist:
        return Decision(
            allowed=False, reason=f"operation_not_allowed:{operation}"
        )
    # Only the top-level keys: the tools take flat arguments.
    identity_argument = next(
        (
            argument_name
            for argument_name in args
            if argument_name in IDENTITY_ARGS
        ),
        None,
    )
    if identity_argument is not None:
        return Decision(
            allowed=False,
            reason=f"object_from_arguments:{identity_argument}",
        )
    return Decision(allowed=True)
