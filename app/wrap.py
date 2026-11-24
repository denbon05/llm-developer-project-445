"""Agent tools over a session's MCP tools, checked against its allowlist."""

from __future__ import annotations

import json
import time
from typing import Any

from agents import FunctionTool, FunctionToolResult, ToolsToFinalOutputResult
from agents.run_context import RunContextWrapper
from mcp.shared.exceptions import MCPError

from actions.contracts import (
    McpErrorCode,
    parse_prepared_action,
    parse_ticket_result,
)
from app import audit, cards, messages, permissions, telemetry
from app.audit import AuditOutcome
from app.pep import PolicyReason
from app.sessions import Session, pep_of
from app.tools import ACTION_BY_PREPARE_TOOL, PREPARE_TOOL_NAMES, ToolName
from core.logging import get_logger, shorten_for_log

logger = get_logger(__name__)

# The error code of a call the allowlist or the policy refused.
DENIED_ERROR = "denied"
# Arguments that retell the subscriber's words: the journal keeps no dialogue.
FREE_TEXT_ARGUMENTS = frozenset({"note"})
# Denials because a service gave no answer: the PDP, or the guardrail on this
# turn. A later try may pass.
SERVICE_UNAVAILABLE_REASONS = frozenset(
    {PolicyReason.PDP_UNAVAILABLE, PolicyReason.GUARD_NOT_OK}
)

# I18N: the answers the subscriber must give.
CARD_NEXT_STEP = (
    "The confirmation card is shown to the subscriber. Report that the card "
    "is ready and that the subscriber must answer да or нет. Nothing has "
    "changed yet."
)


async def create_card(
    session: Session, tool_name: str, args: dict, prepared: dict
) -> dict:
    """Store a prepared action as the session's card if the policy allows it.

    Return what the model reads: the card, or the denial.
    """
    card = cards.build_new_card(
        session, prepared["action"], prepared["details"], prepared["view"]
    )
    # The PDP decides whether this card may exist at all.
    decision = await pep_of(session).prepare(card, guard_ok=session.guard_ok)
    if not decision.allowed:
        # Nothing is stored, so the session's previous pending card stays
        # live.
        return deny_tool_call(session, tool_name, args, decision.reason)
    cards.store_card(session, card)
    logger.info("card_created", action=card.action, pending_id=card.pending_id)
    return {"card_created": True, "card": card.view, "next": CARD_NEXT_STEP}


def check_action_result(tool_name: str, result: dict) -> None:
    """Raise ValidationError if an action server's answer breaks its contract."""
    if tool_name in PREPARE_TOOL_NAMES and result.get("ok"):
        parse_prepared_action(result)
    elif tool_name == ToolName.CREATE_TICKET:
        parse_ticket_result(result)


def find_tool_outcome(tool_output: dict) -> str:
    """Return how a tool call ended: ok, card_created or the error code."""
    if tool_output.get("card_created") is True:
        return "card_created"
    return str(tool_output.get("error", "ok"))


def name_audited_action(tool_name: str) -> str:
    """Return the account action a tool call attempts."""
    # A prepare tool attempts the action its card would run, so one `what`
    # finds every attempt at a change. Any other tool is its own action.
    return ACTION_BY_PREPARE_TOOL.get(tool_name, tool_name)


def remove_free_text_arguments(args: dict) -> dict:
    """Return the arguments without the free text ones."""
    return {
        argument_name: argument_value
        for argument_name, argument_value in args.items()
        if argument_name not in FREE_TEXT_ARGUMENTS
    }


def record_tool_entry(
    session: Session,
    tool_name: str,
    args: dict,
    outcome: AuditOutcome,
    *,
    detail: str,
) -> None:
    """Add a model's tool call to the audit journal."""
    audit.append(
        session.user.sub,
        name_audited_action(tool_name),
        outcome,
        trace_id=session.trace_id,
        session_id=session.session_id,
        args=remove_free_text_arguments(args),
        detail=detail,
    )


def deny_tool_call(
    session: Session, tool_name: str, args: dict, reason: str
) -> dict:
    """Record a refused tool call and return the output for it."""
    logger.warning("tool_call_denied", tool_name=tool_name, reason=reason)
    # The arguments keep any identity key: it shows what was attempted.
    record_tool_entry(
        session, tool_name, args, AuditOutcome.DENIED, detail=reason
    )
    return {"error": DENIED_ERROR, "reason": reason, "retryable": False}


async def call_mcp_tool(
    session: Session, tool_name: str, input_json: str
) -> tuple[dict, dict]:
    """Call an MCP tool for a model; return its arguments and its output."""
    # Parse the arguments the model wrote.
    try:
        args = (
            json.loads(input_json) if input_json and input_json.strip() else {}
        )
    except (TypeError, ValueError):
        args = None
    # Anything but a JSON object, an array included, goes back to the model as
    # an error, so it can repeat the call. Barrier 2 reads the object's keys.
    if not isinstance(args, dict):
        # Nothing was sent: telemetry keeps the text the model wrote.
        return {"raw": input_json}, {
            "error": "bad_arguments",
            "detail": "аргументы — не JSON-объект; повтори вызов по схеме",
        }
    # Barrier 2: the call must be one the session may make, and must not name
    # a subscriber. A denied call never reaches the server.
    decision = permissions.admit(session.allowlist, tool_name, args)
    if not decision.allowed:
        return args, deny_tool_call(session, tool_name, args, decision.reason)
    session.record_tool_use(tool_name)
    # Call the MCP server.
    try:
        result = await session.mcp.call(
            # The key of the server that hosts the tool, e.g. "crm".
            session.mcp.resource_of(tool_name),
            tool_name,
            args,
        )
        # An answer that breaks the action server's contract fails like
        # a server error, below.
        check_action_result(tool_name, result)
    except MCPError as error:
        # The server refused the call: the model gets the code and hints.
        data = error.data if isinstance(error.data, dict) else {}
        error_output = {
            "error": getattr(error, "message", "tool_failed")[:60],
            **data,
        }
        # The action server refuses a prepare call that breaks its
        # contract, a validation error or a business rule alike.
        if (
            tool_name in PREPARE_TOOL_NAMES
            and error.code == McpErrorCode.ACTION_REFUSED
        ):
            record_tool_entry(
                session,
                tool_name,
                args,
                AuditOutcome.REJECTED_BY_CONTRACT,
                detail=error_output["error"],
            )
        return args, error_output
    except Exception as error:  # noqa: BLE001 — сбой сервера или его ответа становится данными для модели
        return args, {"error": "tool_failed", "detail": str(error)[:160]}
    # A prepared action becomes a confirmation card, if the policy allows it.
    if tool_name in PREPARE_TOOL_NAMES and result.get("ok"):
        # The card, not the model, carries the change to the subscriber.
        return args, await create_card(session, tool_name, args, result)
    # A ticket hands the request to an operator: the journal keeps it.
    if tool_name == ToolName.CREATE_TICKET and result.get("ticket_id"):
        record_tool_entry(
            session,
            tool_name,
            args,
            AuditOutcome.ESCALATED,
            detail=str(result["ticket_id"]),
        )
    return args, result


def build_agent_tool(session: Session, tool_name: str) -> FunctionTool:
    """Return the agent tool that calls one of the session's MCP tools."""

    # The agent calls this instead of the MCP tool. Whatever happens, it
    # returns JSON text for the model to read and never raises.
    async def invoke(ctx: RunContextWrapper, input_json: str) -> str:
        started_at = time.perf_counter()
        model_args, tool_output = await call_mcp_tool(
            session, tool_name, input_json
        )
        telemetry.record_tool_call(
            session.trace_id,
            tool_name,
            model_args,
            find_tool_outcome(tool_output),
            round((time.perf_counter() - started_at) * 1000),
        )
        tool_output_json = json.dumps(tool_output, ensure_ascii=False)
        # Development only: the texts hold personal data, so they stay at
        # debug. The output is what the model reads, a card or a denial too.
        logger.debug(
            "tool_call_text",
            tool_name=tool_name,
            args=shorten_for_log(json.dumps(model_args, ensure_ascii=False)),
            output=shorten_for_log(tool_output_json),
        )
        return tool_output_json

    return FunctionTool(
        name=tool_name,
        description=session.mcp.tool_description(tool_name),
        # The JSON schema of the tool's arguments, as its MCP server lists it.
        params_json_schema=session.mcp.tool_schema(tool_name),
        on_invoke_tool=invoke,
    )


def build_agent_tools(
    session: Session, tool_names: list[str]
) -> list[FunctionTool]:
    """Return agent tools for the named tools the session has and may call."""
    # The names come from an agent's spec. A tool this session's MCP servers
    # don't offer is skipped, and so is one its allowlist hides (barrier 1):
    # the model sees only the tools it may really call.
    offered_tool_names = set(session.mcp.tool_names())
    return [
        build_agent_tool(session, tool_name)
        for tool_name in permissions.visible_tools(
            session.allowlist, tool_names
        )
        if tool_name in offered_tool_names
    ]


def read_denial_reason(tool_output: object) -> str | None:
    """Return the reason of a denied call's output, or None for other output."""
    try:
        parsed_output = json.loads(str(tool_output))
    except ValueError:
        return None
    if (
        isinstance(parsed_output, dict)
        and parsed_output.get("error") == DENIED_ERROR
    ):
        return str(parsed_output.get("reason", ""))
    return None


def stop_run_at_denial(
    _context: RunContextWrapper[Any], tool_results: list[FunctionToolResult]
) -> ToolsToFinalOutputResult:
    """End the run with a fixed report once a tool call was denied."""
    # The model never sees the denial, so it can't retry the call with other
    # arguments, such as the same change for the caller's own account.
    for tool_result in tool_results:
        denial_reason = read_denial_reason(tool_result.output)
        if denial_reason is None:
            continue
        # The SDK passes no agent here; in the log this follows the denial
        # and precedes the run's llm_run_completed, which names the agent.
        logger.info("agent_run_stopped", reason=DENIED_ERROR)
        # Without the PDP or the guardrail no change can be made now, which
        # the subscriber may try again later; any other denial is final.
        final_output = (
            messages.CHANGE_UNAVAILABLE
            if denial_reason in SERVICE_UNAVAILABLE_REASONS
            else messages.OPERATION_DENIED
        )
        return ToolsToFinalOutputResult(
            is_final_output=True, final_output=final_output
        )
    # Otherwise the model reads the outputs and goes on.
    return ToolsToFinalOutputResult(is_final_output=False)
