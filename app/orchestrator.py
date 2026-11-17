"""Turn orchestrator: plan → gate → worker agents → editor → SSE events.

A turn is one subscriber message. The orchestrator decides which agents run and
collects the turn's events for the browser. An answer to a live confirmation
card is handled by code, without the model.

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

from actions.contracts import ActionKind, Outcome
from app import cards, idempotency, messages
from app.answers import CardAnswer, classify_card_answer
from app.cards import Card, CardState
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
    propose_plan,
    restrict_plan,
)
from app.sessions import Session
from app.tools import PREPARE_TOOLS, ToolName
from app.workers import build_worker_agents
from core.logging import get_logger

logger = get_logger(__name__)

Emit = Callable[[dict], Awaitable[None]]


# I18N: ticket subjects the operator reads.
TICKET_SUBJECTS: dict[str, str] = {
    ActionKind.TARIFF_CHANGE: "Смена тарифа",
    ActionKind.SERVICE_TOGGLE: "Подключение или отключение услуги",
    ActionKind.CONTACTS_UPDATE: "Смена контактных данных",
}
# I18N: how a card button press appears in the planner history, as if the
# subscriber had typed it.
ANSWER_MESSAGES = {CardAnswer.YES: "да", CardAnswer.NO: "нет"}


async def make_plan(session: Session, message: str) -> Plan:
    """Return the gated plan and record what the gate removed."""
    # One planner per session, built on its first turn.
    if session.planner is None:
        session.planner = build_planner_agent()
    # The gate drops the steps the planner may not ask for, such as an
    # unknown agent or too many steps.
    plan, dropped_steps = restrict_plan(
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
    # Each agent is built once, even if two steps use it.
    agent_ids = tuple(dict.fromkeys(step.agent_id for step in steps))
    worker_agents = build_worker_agents(session, agent_ids)
    # The steps run one after another, in the plan's order.
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
    """Return the editor's reply that cites only the given sources."""
    editor = build_editor_agent()
    editor_input = build_editor_input(message, worker_runs)
    draft = (await run_agent(editor, editor_input)).report
    # In live runs the editor sometimes cited an article no agent had found.
    reply = remove_unknown_citations(draft, sources)
    # The agents found articles, but the reply cites none of them. Prompts
    # alone left about a third of such replies without a citation, so the
    # editor gets one more try: its draft plus the citations to put back.
    if sources and not has_citation(reply):
        repair_input = build_repair_input(editor_input, reply, sources)
        repaired_draft = (await run_agent(editor, repair_input)).report
        # The second try may invent a citation too.
        reply = remove_unknown_citations(repaired_draft, sources)
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
    # Same fields as a turn with a card or a ticket, so every line matches.
    logger.info("turn_finished", sources=len(sources), card="", tickets=0)


def start_trace(session: Session) -> None:
    """Give the session a new trace id and bind it to the log context."""
    session.trace_id = f"tr_{uuid.uuid4().hex[:12]}"
    bind_contextvars(trace_id=session.trace_id, session_id=session.session_id)


async def call_execute_action(
    session: Session, card: Card, idempotency_key: str
) -> dict:
    """Call the action server to execute the card."""
    return await session.mcp.call(
        session.mcp.resource_of(ToolName.EXECUTE_ACTION),
        ToolName.EXECUTE_ACTION,
        {
            # Code makes this call, not the tool wrapper, so it passes the
            # subject itself.
            "sub": session.user.sub,
            "action": card.action,
            "details_json": json.dumps(card.details, ensure_ascii=False),
            "pending_id": card.pending_id,
            "idempotency_key": idempotency_key,
        },
    )


# POST-QA: rename to execute_confirmed_card. tests/smoke.py calls this name.
async def execute_pending(session: Session, card: Card) -> str:
    """Execute a confirmed card at most once and return the reply."""
    # One card is one operation: every "да" for it reuses this key.
    idempotency_key = f"{card.action}:{card.pending_id}"
    # idempotency.execute calls the action server only if the key has no
    # stored result yet.
    try:
        stored_result = await idempotency.execute(
            idempotency_key,
            session.user.sub,
            card.action,
            lambda: call_execute_action(session, card, idempotency_key),
        )
    except Exception as error:  # noqa: BLE001 -- any failure must end as a reply
        logger.error(
            "action_failed",
            action=card.action,
            pending_id=card.pending_id,
            error_type=type(error).__name__,
        )
        # The card stays pending and the key stays free, so the next "да"
        # tries again with the same key.
        return messages.ACTION_FAILED
    # A repeat gets the duplicate reply, whatever the first run returned.
    result = (
        {"outcome": Outcome.DUPLICATE}
        if stored_result.is_duplicate
        else stored_result.result
    )
    # The card is confirmed only once the account has the change. After a
    # refusal (daily limit, a tariff closed meanwhile) or an unknown outcome
    # it stays pending: while it lives, the subscriber may answer it again or
    # hand it to an operator with its details.
    if result.get("outcome") in idempotency.ACCOUNT_CHANGED_OUTCOMES:
        cards.settle_card(card, CardState.CONFIRMED)
    logger.info(
        "action_executed",
        action=card.action,
        pending_id=card.pending_id,
        outcome=str(result.get("outcome", "")),
    )
    # Code writes the reply from the outcome, no model retells it.
    return messages.build_execution_reply(result)


async def escalate_card(session: Session, card: Card) -> str:
    """Hand the card's action to an operator and return the reply."""
    # The ticket's note lists the card's rows, so the operator sees what was
    # offered.
    ticket = await session.mcp.call(
        session.mcp.resource_of(ToolName.CREATE_TICKET),
        ToolName.CREATE_TICKET,
        {
            "sub": session.user.sub,
            "subject": TICKET_SUBJECTS.get(card.action, card.action),
            "note": "; ".join(
                f"{label}: {value}" for label, value in card.view.items()
            ),
        },
    )
    ticket_id = ticket.get("ticket_id")
    if not ticket_id:
        # No ticket was opened: the card stays live and the subscriber may
        # ask again.
        logger.error("ticket_failed", pending_id=card.pending_id)
        return messages.TICKET_FAILED
    # Rejected only once the ticket exists, so a failure leaves the card live.
    cards.settle_card(card, CardState.REJECTED)
    logger.info("ticket_created", ticket_id=ticket_id)
    return messages.TICKET_CREATED.format(ticket_id=ticket_id)


async def apply_card_answer(
    session: Session, card: Card | None, answer: CardAnswer
) -> str | None:
    """Carry out an answer about the card and return the reply.

    Return None when code has nothing to do: the agents take a typed message,
    and the card endpoint answers 404.
    """
    if card is None:
        return None
    # A second "да" for a card confirmed a moment ago, typed or pressed. Its
    # key holds the result, so the reply is «уже исполнено» and nothing runs
    # twice. Once the card expired, a "да" no longer refers to it.
    # POST-QA: drop is_repeated_yes for the simple flow: only a live card
    # takes answers, a later "да" goes to the agents, the idempotency key
    # still stops a repeat, and the web chat disables the card's buttons once
    # the card is answered.
    is_repeated_yes = (
        answer is CardAnswer.YES
        and card.state == CardState.CONFIRMED
        and not card.is_expired
    )
    # Rejected, replaced and expired cards take no answers.
    if not card.is_live and not is_repeated_yes:
        return None

    logger.info(
        "card_decision",
        action=card.action,
        pending_id=card.pending_id,
        answer=str(answer),
    )
    if answer is CardAnswer.TOPIC:
        # Not an answer to the card: the agents take the message and the card
        # stays live.
        return None
    # "да" executes the card, "позови оператора" turns it into a ticket.
    if answer is CardAnswer.YES:
        return await execute_pending(session, card)
    if answer is CardAnswer.OPERATOR:
        return await escalate_card(session, card)
    # "нет" cancels the card.
    cards.settle_card(card, CardState.REJECTED)
    return messages.CARD_REJECTED


async def apply_message_to_card(
    session: Session, message: str, latest_card: Card | None
) -> str | None:
    """Return the reply if the message answers the card, or None if not."""
    # Code reads the message, no model: yes, no, operator or another topic.
    answer = classify_card_answer(message)
    return await apply_card_answer(session, latest_card, answer)


def find_new_card(
    latest_card_before: Card | None, latest_card_after: Card | None
) -> Card | None:
    """Return the newest card after the agents if they created it, else None."""
    # The agents created a card if the session's newest card changed while
    # they ran. It may already be expired: the turn says so instead of
    # showing it.
    if latest_card_after is None or (
        latest_card_before is not None
        and latest_card_after.pending_id == latest_card_before.pending_id
    ):
        return None
    return latest_card_after


def parse_tool_output(output: str) -> dict:
    """Return a tool output as a dict, or an empty one if it isn't JSON."""
    try:
        parsed_output = json.loads(output)
    except ValueError:
        return {}
    return parsed_output if isinstance(parsed_output, dict) else {}


def has_created_card(run: AgentRun) -> bool:
    """Return whether the agent's run created a confirmation card."""
    # The tool wrapper adds "card_created" to a prepare tool's output when it
    # stored the card.
    return any(
        tool_call.name in PREPARE_TOOLS
        and parse_tool_output(tool_call.output).get("card_created") is True
        for tool_call in run.tool_calls
    )


def find_ticket_id(run: AgentRun) -> str | None:
    """Return the id of the ticket the agent's run created, if any."""
    for tool_call in run.tool_calls:
        if tool_call.name == ToolName.CREATE_TICKET:
            ticket_id = parse_tool_output(tool_call.output).get("ticket_id")
            if ticket_id:
                return str(ticket_id)
    return None


def has_created_card_or_ticket(run: AgentRun) -> bool:
    """Return whether the agent's run created a card or a ticket."""
    return has_created_card(run) or find_ticket_id(run) is not None


async def send_card_and_ticket_replies(
    session: Session,
    message: str,
    worker_runs: list[AgentRun],
    card: Card | None,
    ticket_ids: list[str],
    emit: Emit,
) -> None:
    """Send the replies of a turn whose agents created a card or tickets."""
    # A model must not retell card prices or ticket ids: small models distort
    # them. So the runs that created a card or a ticket skip the editor, and
    # code writes fixed texts for them below.
    editor_runs = [
        run for run in worker_runs if not has_created_card_or_ticket(run)
    ]
    # Every text sent in this turn, for the dialogue history.
    sent_texts = []
    sources = []

    # 1. The other runs, for example an answer to a question asked in the
    # same message, go to the editor as in any turn.
    if editor_runs:
        sources = collect_kb_sources(editor_runs)
        editor_reply = await write_reply(message, editor_runs, sources)
        await emit({"type": "reply", "text": editor_reply, "sources": sources})
        sent_texts.append(editor_reply)

    # 2. A fixed text for each ticket, with the id from the tool output.
    for ticket_id in ticket_ids:
        ticket_text = messages.TICKET_CREATED.format(ticket_id=ticket_id)
        await emit({"type": "reply", "text": ticket_text, "sources": []})
        sent_texts.append(ticket_text)

    # 3. The card, read again: the editor may have taken a while, so the
    # card's state and the seconds it has left must be current.
    current_card = (
        cards.find_card(session.session_id, card.pending_id) if card else None
    )
    shown_card_id = ""
    if current_card is not None and current_card.is_live:
        # The card, then a fixed text that asks to confirm or cancel it.
        await emit(
            {
                "type": "card",
                "pending_id": current_card.pending_id,
                "card": current_card.public_view(),
            }
        )
        await emit(
            {"type": "reply", "text": messages.CARD_READY, "sources": []}
        )
        sent_texts.append(messages.CARD_READY)
        shown_card_id = current_card.pending_id
    elif (
        current_card is not None
        and current_card.state == CardState.PENDING
        and current_card.is_expired
    ):
        # A slow turn outlived the card: it can't be answered any more, so
        # it isn't shown, and the subscriber learns why. Other states (a
        # refusal by the policy or the guardrail of later steps) need their
        # own reply.
        logger.info("card_expired", pending_id=current_card.pending_id)
        await emit(
            {"type": "reply", "text": messages.CARD_EXPIRED, "sources": []}
        )
        sent_texts.append(messages.CARD_EXPIRED)

    # The planner sees the turn as one reply: the sent texts joined.
    session.remember_turn(message, "\n\n".join(sent_texts))
    logger.info(
        "turn_finished",
        sources=len(sources),
        card=shown_card_id,
        tickets=len(ticket_ids),
    )


async def run_agents(
    session: Session, message: str, latest_card: Card | None, emit: Emit
) -> None:
    """Plan the turn, run the worker agents and send the replies."""
    plan = await make_plan(session, message)
    # The planner needs more details: its question is the whole reply.
    if plan.question:
        await send_reply(session, message, plan.question, [], emit)
        return
    # Show the browser which agents work on what.
    if plan.steps:
        await emit(build_plan_event(plan))

    worker_runs = await run_steps(session, plan.steps)

    # Did the agents create a card or tickets? Then code writes those parts
    # of the reply, and the editor only the rest.
    new_card = find_new_card(
        latest_card, cards.find_latest_card(session.session_id)
    )
    ticket_ids = [
        ticket_id for run in worker_runs if (ticket_id := find_ticket_id(run))
    ]
    if new_card is not None or ticket_ids:
        await send_card_and_ticket_replies(
            session, message, worker_runs, new_card, ticket_ids, emit
        )
        return

    # Nothing was created: the editor writes the whole reply.
    sources = collect_kb_sources(worker_runs)
    reply = await write_reply(message, worker_runs, sources)
    await send_reply(session, message, reply, sources, emit)


async def run_turn(session: Session, message: str, emit: Emit) -> None:
    """Answer one subscriber message, reporting progress through emit."""
    # One turn at a time per session. A card button press waits here too.
    async with session.turn_lock:
        start_trace(session)
        logger.info("turn_started")
        latest_card = cards.find_latest_card(session.session_id)
        # First code checks whether the message answers a card.
        reply = await apply_message_to_card(session, message, latest_card)
        if reply is not None:
            # It answered the card: code's reply is the whole answer, no
            # agent runs.
            await send_reply(session, message, reply, [], emit)
            return
        # Anything else goes to the agents, also a question asked while a
        # card waits.
        await run_agents(session, message, latest_card, emit)


async def decide_card(
    session: Session, pending_id: str, answer: CardAnswer
) -> str | None:
    """Apply an answer to the session's card with this id and return the reply.

    Return None if the session has no such card to answer.
    """
    # The same lock as a turn, so an answer never runs in the middle of one.
    async with session.turn_lock:
        start_trace(session)
        # The id names the card exactly; the same rules as for a typed answer
        # decide what the press does.
        card = cards.find_card(session.session_id, pending_id)
        reply = await apply_card_answer(session, card, answer)
        if reply is None:
            return None
        # The planner sees the press as a typed "да" or "нет" and its reply.
        session.remember_turn(ANSWER_MESSAGES[answer], reply)
        return reply
