"""Обёртки MCP-инструментов для агентов: вызов с JWT + учёт трейса.

Готовая основа: MCP-инструмент превращается в FunctionTool агента;
субъект вызова в Bearer-токене соединения; кривой JSON аргументов —
машинно-читаемый ответ модели, сессия не роняется; ошибка MCP-сервера
(MCPError, код + data с подсказками) — JSON tool-результат для модели.

TODO(права): два барьера —
  wrap_tools: фильтровать имена через permissions.visible_tools
  (барьер 1: модель не видит запрещённого);
  invoke: permissions.admit(session.allowlist, name, args) перед вызовом
  (барьер 2: отказ → {"error": "denied", "reason"} + запись в аудит);
  подготовленную карточку проводить через pep.prepare — Deny → карточки
  нет, модель получает отказ с reason.
"""

from __future__ import annotations

import json
import time

from agents import FunctionTool
from agents.run_context import RunContextWrapper
from mcp.shared.exceptions import MCPError

from actions.contracts import McpErrorCode
from app import audit, cards, telemetry
from app.audit import AuditOutcome
from app.sessions import Session
from app.tools import ACTION_BY_PREPARE_TOOL, PREPARE_TOOL_NAMES, ToolName
from core.logging import get_logger

logger = get_logger(__name__)

SUBJECT_ARGUMENT = "sub"

# I18N: the answers the subscriber must give.
CARD_NEXT_STEP = (
    "The confirmation card is shown to the subscriber. Report that the card "
    "is ready and that the subscriber must answer да or нет. Nothing has "
    "changed yet."
)


def hide_subject(schema: dict) -> dict:
    """Return the tool schema without the subject argument."""
    properties = {
        argument_name: argument_schema
        for argument_name, argument_schema in schema.get(
            "properties", {}
        ).items()
        if argument_name != SUBJECT_ARGUMENT
    }
    required = [
        argument_name
        for argument_name in schema.get("required", [])
        if argument_name != SUBJECT_ARGUMENT
    ]
    return {**schema, "properties": properties, "required": required}


def create_card(session: Session, prepared: dict) -> dict:
    """Store a prepared action as the session's card and tell the model."""
    card = cards.create(
        session, prepared["action"], prepared["details"], prepared["view"]
    )
    logger.info("card_created", action=card.action, pending_id=card.pending_id)
    return {"card_created": True, "card": card.view, "next": CARD_NEXT_STEP}


def find_tool_outcome(tool_output: dict) -> str:
    """Return how a tool call ended: ok, card_created or the error code."""
    if tool_output.get("card_created") is True:
        return "card_created"
    return str(tool_output.get("error", "ok"))


def record_refused_prepare(
    session: Session, tool_name: str, args: dict, error_code: str
) -> None:
    """Add the refusal of a prepare tool to the audit journal."""
    audit.append(
        session.user.sub,
        ACTION_BY_PREPARE_TOOL[tool_name],
        AuditOutcome.REJECTED_BY_CONTRACT,
        trace_id=session.trace_id,
        session_id=session.session_id,
        args=args,
        detail=error_code,
    )


def record_ticket(session: Session, args: dict, ticket_id: str) -> None:
    """Add a created ticket to the audit journal."""
    audit.append(
        session.user.sub,
        ToolName.CREATE_TICKET,
        AuditOutcome.ESCALATED,
        trace_id=session.trace_id,
        session_id=session.session_id,
        # The note retells the subscriber's words, and the journal keeps no
        # message text.
        args={"subject": args.get("subject")},
        detail=ticket_id,
    )


def wrap_mcp_tool(session: Session, tool_name: str) -> FunctionTool:
    """Обёртка одного MCP-инструмента для агента этой сессии."""
    # The JSON schema of the tool's arguments, as its MCP server lists it.
    schema = session.mcp.tool_schema(tool_name)
    takes_subject = SUBJECT_ARGUMENT in schema.get("properties", {})

    # Returns the arguments sent to the server and the output for the model.
    async def call_tool(input_json: str) -> tuple[dict, dict]:
        # Parse the arguments the model wrote. Broken JSON goes back to the
        # model as an error, so it can repeat the call.
        try:
            args = (
                json.loads(input_json)
                if input_json and input_json.strip()
                else {}
            )
        except (TypeError, ValueError):
            # Nothing was sent: telemetry keeps the text the model wrote.
            return {"raw": input_json}, {
                "error": "bad_arguments",
                "detail": "аргументы — не JSON; повтори вызов по схеме",
            }
        # Add the subscriber.
        if takes_subject:
            # A deliberate temporary hole: the action server trusts `sub` from
            # the arguments until it reads the subject from the token. The
            # model never sees the argument, and anything it sends is
            # overwritten with this session's subscriber.
            args[SUBJECT_ARGUMENT] = session.user.sub
        session.record_tool_use(tool_name)
        # Call the MCP server.
        try:
            result = await session.mcp.call(
                # The key of the server that hosts the tool, e.g. "crm".
                session.mcp.resource_of(tool_name),
                tool_name,
                args,
            )
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
                record_refused_prepare(
                    session, tool_name, args, error_output["error"]
                )
            return args, error_output
        except Exception as error:  # noqa: BLE001 — сбой сервера становится данными для модели
            return args, {"error": "tool_failed", "detail": str(error)[:160]}
        # A prepared action becomes a confirmation card.
        if tool_name in PREPARE_TOOL_NAMES and result.get("ok"):
            # The card, not the model, carries the change to the subscriber.
            return args, create_card(session, result)
        # A ticket hands the request to an operator: the journal keeps it.
        if tool_name == ToolName.CREATE_TICKET and result.get("ticket_id"):
            record_ticket(session, args, str(result["ticket_id"]))
        return args, result

    # The agent calls this instead of the MCP tool. Whatever happens, it
    # returns JSON text for the model to read and never raises.
    async def invoke(ctx: RunContextWrapper, input_json: str) -> str:
        started_at = time.perf_counter()
        sent_args, tool_output = await call_tool(input_json)
        telemetry.record_tool_call(
            session.trace_id,
            tool_name,
            sent_args,
            find_tool_outcome(tool_output),
            round((time.perf_counter() - started_at) * 1000),
        )
        return json.dumps(tool_output, ensure_ascii=False)

    return FunctionTool(
        name=tool_name,
        description=session.mcp.tool_description(tool_name),
        params_json_schema=hide_subject(schema) if takes_subject else schema,
        on_invoke_tool=invoke,
    )


def wrap_tools(session: Session, names: list[str]) -> list[FunctionTool]:
    """Собрать обёртки для перечня имён (какие имена давать — решает код)."""
    # The names come from an agent's spec and may include tools this session's
    # MCP servers don't offer. Those are skipped, so the agent gets only the
    # tools it can really call.
    session_tools = set(session.mcp.tool_names())
    return [
        wrap_mcp_tool(session, tool_name)
        for tool_name in names
        if tool_name in session_tools
    ]
