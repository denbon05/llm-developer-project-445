"""Turn orchestrator: plan → gate → worker agents → editor → SSE events.

A turn is one subscriber message. The orchestrator decides which agents run and
collects the turn's events for the browser.

TODO(контракты) — карточка действия:
  - в начале хода: cards.active(session) → cards.classify_answer(message):
    «да» → execute_pending, «нет» → settle("rejected") + «отменяю»,
    «оператор» → escalate, смена темы → обычный ход с живой карточкой;
  - после агентов: свежая карточка → событие {"type": "card"} и ход
    завершается (ждём подтверждение);
  - execute_pending(session, card): settle("confirmed"), ключ
    f"{action}:{pending_id}", idempotency.execute → вызов MCP execute_action
    → человекочитаемый ответ (executed / duplicate / daily_limit / отказ).

TODO(аудит) — аудит: audit.append на все исходы пути действия
  (executed с before/after, duplicate_suppressed, rejected_by_user,
  server_limit, escalated).

TODO(права) — права: «да» → PEP: pep.confirm → pep.execute; при Deny —
  человекочитаемый отказ по reason (session_limit, confirmation_expired,
  card_not_prepared, pdp_unavailable).

TODO(guardrail) — guardrail: input-проверка реплики до всего хода (unsafe →
  отказ, запись в аудит); output-проверка ответа и карточки до показа;
  недоступный сервис → session.guard_state["ok"] = False (fail-closed
  для действий), чтение продолжается.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Sequence

from structlog.contextvars import bind_contextvars

from app.editor import (
    build_editor_agent,
    build_editor_input,
    build_repair_input,
    has_citation,
    remove_unknown_citations,
)
from app.llm import AgentRun, run_agent
from app.planner import (
    Plan,
    Step,
    build_planner_agent,
    gate,
    propose_plan,
)
from app.sessions import Session
from app.tools import ToolName
from app.workers import build_worker_agents
from core.logging import get_logger

logger = get_logger(__name__)

Emit = Callable[[dict], Awaitable[None]]


async def make_plan(session: Session, message: str) -> Plan:
    """Return the gated plan and record what the gate removed."""
    if session.planner is None:
        session.planner = build_planner_agent()
    plan, dropped_steps = gate(
        await propose_plan(session.planner, session.planner_messages, message)
    )
    session.dropped_steps += dropped_steps
    logger.info(
        "plan_ready",
        agents=[step.agent_id for step in plan.steps],
        dropped_steps=[
            f"{dropped.agent_id}:{dropped.reason}" for dropped in dropped_steps
        ],
    )
    return plan


def build_plan_event(plan: Plan) -> dict:
    """Return the event that shows the plan to the browser."""
    return {
        "type": "plan",
        # I18N: tasks are Russian, so the scenarios' substring search for an
        # agent id can't match a task by accident. In English it could.
        "agents": [f"[{step.agent_id}] {step.task}" for step in plan.steps],
    }


async def run_steps(session: Session, steps: list[Step]) -> list[AgentRun]:
    """Run each step's agent. Agents see only their own task and tools."""
    agent_ids = tuple(dict.fromkeys(step.agent_id for step in steps))
    worker_agents = build_worker_agents(session, agent_ids)
    return [
        await run_agent(worker_agents[step.agent_id], step.task)
        for step in steps
    ]


def collect_kb_sources(worker_runs: Sequence[AgentRun]) -> list[str]:
    """Return the knowledge base articles the agents found, without repeats."""
    found = [
        article["source"]
        for run in worker_runs
        for call in run.tool_calls
        if call.name == ToolName.SEARCH_KB
        for article in json.loads(call.output).get("results", [])
    ]
    return list(dict.fromkeys(found))


async def write_reply(
    message: str, worker_runs: Sequence[AgentRun], sources: Sequence[str]
) -> str:
    """Return the editor's reply with only real citations.

    In live runs prompts alone left a third of the replies that needed a
    citation without one, and invented sources in others.
    """
    editor = build_editor_agent()
    editor_input = build_editor_input(message, worker_runs)
    draft = (await run_agent(editor, editor_input)).report
    reply = remove_unknown_citations(draft, sources)
    if sources and not has_citation(reply):
        repair_input = build_repair_input(editor_input, reply, sources)
        repaired = (await run_agent(editor, repair_input)).report
        reply = remove_unknown_citations(repaired, sources)
    return reply


async def send_reply(
    session: Session,
    message: str,
    text: str,
    sources: list[str],
    emit: Emit,
) -> None:
    """Send the reply and only then add the turn to the history."""
    await emit({"type": "reply", "text": text, "sources": sources})
    session.remember_turn(message, text)
    logger.info("turn_finished", sources=len(sources))


async def run_turn(session: Session, message: str, emit: Emit) -> None:
    """Answer one subscriber message, reporting progress through emit."""
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"
    bind_contextvars(trace_id=session.trace_id, session_id=session.session_id)
    logger.info("turn_started")

    plan = await make_plan(session, message)
    if plan.question:
        await send_reply(session, message, plan.question, [], emit)
        return
    if plan.steps:
        await emit(build_plan_event(plan))

    worker_runs = await run_steps(session, plan.steps)
    sources = collect_kb_sources(worker_runs)
    reply = await write_reply(message, worker_runs, sources)
    await send_reply(session, message, reply, sources, emit)
