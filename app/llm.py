"""The single boundary every LLM run passes: agents are built and run here.

TODO(failover): если llm_pool.enabled() — запуск через
  llm_pool.run_with_failover, в llm_calls — фактический провайдер.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, cast

from agents import (
    Agent,
    AgentOutputSchema,
    Runner,
    Tool,
    ToolCallItem,
    ToolCallOutputItem,
    ToolsToFinalOutputFunction,
    TResponseInputItem,
)
from agents.result import RunResult
from pydantic import BaseModel

from app import telemetry
from core import config
from core.logging import get_logger, read_trace_id, shorten_for_log

logger = get_logger(__name__)

# The data agent calling its four tools one by one, a final report and one
# retry after a bad call.
MAX_AGENT_TURNS = 6


@dataclass(frozen=True)
class ToolCall:
    name: str
    output: str


@dataclass(frozen=True)
class AgentRun:
    """The outcome of one agent run."""

    output: Any  # text, or an instance of the agent's output_type
    tool_calls: list[ToolCall]
    seconds: float

    @property
    def report(self) -> str:
        return str(self.output).strip()


def build_agent(
    name: str,
    instructions: str,
    tools: list[Tool] | None = None,
    output_type: type | None = None,
    # What happens after each tool step. The SDK's default lets the model read
    # the tool outputs and go on; a function can end the run there instead.
    tool_use_behavior: ToolsToFinalOutputFunction
    | Literal["run_llm_again"] = "run_llm_again",
) -> Agent:
    """Return an agent on the configured model."""
    return Agent(
        name=name,
        instructions=instructions,
        tools=tools or [],
        model=config.MODEL_NAME,
        # Strict mode is too demanding for small models: plans fail validation.
        output_type=(
            AgentOutputSchema(output_type, strict_json_schema=False)
            if output_type
            else None
        ),
        tool_use_behavior=tool_use_behavior,
    )


def collect_tool_calls(result: RunResult) -> list[ToolCall]:
    """Return the run's tool calls with their outputs, in call order."""
    names = {
        item.call_id: item.tool_name
        for item in result.new_items
        if isinstance(item, ToolCallItem)
    }
    tool_calls = []
    for item in result.new_items:
        if isinstance(item, ToolCallOutputItem):
            # Our tools are all function tools, whose outputs are dicts.
            call_id = cast(dict[str, Any], item.raw_item)["call_id"]
            tool_calls.append(ToolCall(str(names[call_id]), str(item.output)))
    return tool_calls


def read_latest_input(agent_input: str | list[TResponseInputItem]) -> str:
    """Return the agent's input, or the newest message of a dialogue."""
    if isinstance(agent_input, str):
        return agent_input
    # The planner gets the whole dialogue; its newest message is the turn's.
    newest_item = cast(dict[str, Any], agent_input[-1]) if agent_input else {}
    return str(newest_item.get("content", ""))


def format_output(final_output: object) -> str:
    """Return an agent's final output as text: a plan becomes its JSON."""
    if isinstance(final_output, BaseModel):
        return final_output.model_dump_json()
    return str(final_output)


async def run_agent(
    agent: Agent, agent_input: str | list[TResponseInputItem]
) -> AgentRun:
    """Run one agent to its final output."""
    started_at = time.perf_counter()
    result = await Runner.run(agent, agent_input, max_turns=MAX_AGENT_TURNS)
    elapsed_seconds = time.perf_counter() - started_at
    seconds = round(elapsed_seconds, 2)
    tool_calls = collect_tool_calls(result)
    usage = result.context_wrapper.usage
    # The trace id comes from the log context, so callers don't pass it.
    telemetry.record_llm_call(
        read_trace_id(),
        agent.name,
        str(agent.model),
        usage.input_tokens,
        usage.output_tokens,
        round(elapsed_seconds * 1000),
    )
    if not str(result.final_output).strip():
        # After a tool call small models sometimes end with an empty message.
        logger.warning("llm_run_empty_output", agent=agent.name)
    logger.info(
        "llm_run_completed",
        agent=agent.name,
        seconds=seconds,
        llm_calls=len(result.raw_responses),
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        tools=[call.name for call in tool_calls],
    )
    # Development only: the texts hold personal data, so they stay at debug.
    logger.debug(
        "llm_run_text",
        agent=agent.name,
        input=shorten_for_log(read_latest_input(agent_input)),
        output=shorten_for_log(format_output(result.final_output)),
    )
    return AgentRun(result.final_output, tool_calls, seconds)
