"""Tool names must match what the MCP servers expose. No services are needed."""

import pytest

from app.tools import ToolName
from services import billing, crm, kb


@pytest.mark.parametrize("tool_name", ToolName)
def test_tool_exists_on_a_server(tool_name):
    """wrap_tools skips unknown names, so a typo would go unnoticed."""
    assert any(hasattr(server, tool_name) for server in (crm, billing, kb))
