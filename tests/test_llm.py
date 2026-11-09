"""What an agent run reports. No services or model are needed."""

from app.llm import AgentRun, ToolCall
from app.tools import ToolName


def test_report_is_the_agents_text():
    """The report is what the agent wrote, not what its tools returned."""
    tool_call = ToolCall(ToolName.GET_MY_TARIFF, "{}")
    run = AgentRun("Тариф Базовый", [tool_call], 1.0)

    assert run.report == "Тариф Базовый"


def test_blank_output_is_an_empty_report():
    """Tool results never replace a report, even when the agent wrote nothing."""
    run = AgentRun("  ", [ToolCall(ToolName.SEARCH_KB, '{"results": []}')], 1.0)

    assert run.report == ""
