"""Which prompt each worker agent gets for what its session may do."""

import pytest

from app import permissions, workers
from app.tools import ToolName
from core import config


def find_visible_tool_names(login: str) -> frozenset[str]:
    """Return the tools a demo user's session shows its models."""
    user = config.USERS[login]
    allowlist = permissions.allowlist_from_claims(
        {"role": user.role, "scope": " ".join(user.scopes)}
    )
    return frozenset(permissions.visible_tools(allowlist, list(ToolName)))


@pytest.mark.parametrize(
    ("login", "agent_id", "expected_prompt"),
    [
        # May prepare cards: the usual change agent.
        ("anna", "tariff", workers.TARIFF_PROMPT),
        # No card for boris: changes are unavailable, an operator can help.
        ("boris", "tariff", workers.TARIFF_NO_ACTIONS_PROMPT),
        # A client's data agent refuses other subscribers' ids.
        ("anna", "data", workers.DATA_PROMPT),
        # An operator's data agent reads other subscribers' overviews.
        ("olga", "data", workers.OPERATOR_DATA_PROMPT),
    ],
)
def test_prompt_follows_the_visible_tools(login, agent_id, expected_prompt):
    """The session's tools, not its role, choose the prompt."""
    assert (
        workers.choose_prompt(agent_id, find_visible_tool_names(login))
        == expected_prompt
    )
