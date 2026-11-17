"""Обёртки MCP-инструментов для агентов: вызов с JWT + учёт трейса.

Готовая основа: MCP-инструмент превращается в FunctionTool агента;
субъект вызова в Bearer-токене соединения; кривой JSON аргументов —
машинно-читаемый ответ модели, сессия не роняется; ошибка MCP-сервера
(MCPError, код + data с подсказками) — JSON tool-результат для модели.

TODO(аудит): каждый tool-вызов модели → таблицу tool_calls
  (trace_id, tool, args — обрезанные, outcome, latency_ms).

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

from agents import FunctionTool
from agents.run_context import RunContextWrapper
from mcp.shared.exceptions import MCPError

from app import cards
from app.sessions import Session
from app.tools import PREPARE_TOOLS
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


def wrap_mcp_tool(session: Session, name: str) -> FunctionTool:
    """Обёртка одного MCP-инструмента для агента этой сессии."""
    schema = session.mcp.tool_schema(name)
    takes_subject = SUBJECT_ARGUMENT in schema.get("properties", {})

    # The agent calls this instead of the MCP tool. Whatever happens, it
    # returns JSON text for the model to read and never raises.
    async def invoke(ctx: RunContextWrapper, input_json: str) -> str:
        # 1. Parse the arguments the model wrote. Broken JSON goes back to the
        # model as an error, so it can repeat the call.
        try:
            args = (
                json.loads(input_json)
                if input_json and input_json.strip()
                else {}
            )
        except (TypeError, ValueError):
            return json.dumps(
                {
                    "error": "bad_arguments",
                    "detail": "аргументы — не JSON; повтори вызов по схеме",
                },
                ensure_ascii=False,
            )
        # 2. Add the subscriber.
        if takes_subject:
            # A deliberate temporary hole: the action server trusts `sub` from
            # the arguments until it reads the subject from the token. The
            # model never sees the argument, and anything it sends is
            # overwritten with this session's subscriber.
            args[SUBJECT_ARGUMENT] = session.user.sub
        session.record_tool_use(name)
        # 3. Call the MCP server.
        try:
            result = await session.mcp.call(
                session.mcp.resource_of(name), name, args
            )
        except MCPError as error:
            # The server refused the call: the model gets the code and hints.
            data = error.data if isinstance(error.data, dict) else {}
            return json.dumps(
                {
                    "error": getattr(error, "message", "tool_failed")[:60],
                    **data,
                },
                ensure_ascii=False,
            )
        except Exception as error:  # noqa: BLE001 — сбой сервера становится данными для модели
            return json.dumps(
                {"error": "tool_failed", "detail": str(error)[:160]},
                ensure_ascii=False,
            )
        # 4. A prepared action becomes a confirmation card.
        if name in PREPARE_TOOLS and result.get("ok"):
            # The card, not the model, carries the change to the subscriber.
            result = create_card(session, result)
        return json.dumps(result, ensure_ascii=False)

    return FunctionTool(
        name=name,
        description=session.mcp.tool_description(name),
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
