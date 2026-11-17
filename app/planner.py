"""Planner: proposes which worker agents a turn runs, before anything executes.

The plan is LLM output, so execution doesn't trust it: restrict_plan() keeps only what
code allows. Changes to a tariff, service or contacts go through a confirmation
card and the permission policy, never straight from a plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agents import Agent, TResponseInputItem
from pydantic import AliasChoices, BaseModel, Field, field_validator

from app.llm import build_agent, run_agent
from app.workers import AGENT_SPECS

# Only these agents may keep write access.
ACTION_AGENTS = ("tariff", "tickets")
MAX_PLAN_STEPS = 4

AGENT_LIST = "\n".join(
    f"- {agent_id}: {spec.description}"
    for agent_id, spec in AGENT_SPECS.items()
)

# I18N: the language of tasks, of the clarifying question and of the examples.
PLANNER_RULES = f"""You are the planner of a mobile operator's support chat.
You never answer the subscriber. You decide which agents to run and what each
one must do.

Agents (agent_id):
{AGENT_LIST}

Rules:
- Write reasoning first: one or two sentences on what the subscriber needs and
  which agents that takes.
- Add a step for an agent only if its description covers part of the request.
- If the request needs both facts about this subscriber and a general rule, give
  each its own step, and keep the subscriber's own numbers out of the rule task.
- A greeting, or a request no agent can handle: no steps.
- Use the conversation to resolve references like "the second one" or "it", and
  name the concrete thing in the task.
- Write each task in Russian as one short instruction. Write tariff names in
  lowercase without quotes.
- access_mode is "read" unless the step changes data.
- If the request is too unclear to plan, leave steps empty and put a clarifying
  question in Russian in question.
"""

PLANNER_EXAMPLES = """
Examples:
Message: какой адрес указан в моём договоре?
{"reasoning": "Нужны личные данные этого абонента.", "steps": [{"agent_id": "data", "task": "узнать адрес абонента", "access_mode": "read"}]}
Message: можно ли приостановить номер на время отпуска?
{"reasoning": "Вопрос об общих правилах.", "steps": [{"agent_id": "kb", "task": "найти правила приостановки номера", "access_mode": "read"}]}
Message: почему в этом месяце счёт больше обычного?
{"reasoning": "Нужны списания абонента и правила начисления.", "steps": [{"agent_id": "data", "task": "показать последние списания", "access_mode": "read"}, {"agent_id": "kb", "task": "найти правила начисления платы", "access_mode": "read"}]}
Message: хочу перейти на игровой тариф
{"reasoning": "Абонент просит сменить тариф.", "steps": [{"agent_id": "tariff", "task": "сменить тариф абонента на игровой", "access_mode": "write"}]}
Message: соедините меня с живым человеком
{"reasoning": "Абонент просит связать его с сотрудником.", "steps": [{"agent_id": "tickets", "task": "передать обращение оператору: абонент просит связаться с ним", "access_mode": "write"}]}
Message: спасибо!
{"reasoning": "Благодарность, агенты не нужны.", "steps": []}
"""

PLANNER_PROMPT = PLANNER_RULES + PLANNER_EXAMPLES


class AccessMode(StrEnum):
    READ = "read"
    WRITE = "write"


class Step(BaseModel):
    # Small models answer in their own dialect of field names.
    agent_id: str = Field(
        validation_alias=AliasChoices("agent_id", "agent", "agentName")
    )
    task: str = Field(validation_alias=AliasChoices("task", "job"))
    access_mode: AccessMode = AccessMode.READ

    @field_validator("access_mode", mode="before")
    @classmethod
    def treat_unknown_as_read(cls, value: object) -> AccessMode:
        # The gate has the last word, so a strange value must not fail a plan.
        return (
            AccessMode.WRITE if value == AccessMode.WRITE else AccessMode.READ
        )


class Plan(BaseModel):
    # First on purpose: the model writes JSON in field order, so this text is
    # written before the decision and improves it.
    reasoning: str
    # Required on purpose: with a default, a reply in the model's own shape
    # parsed as a silently empty plan.
    steps: list[Step]
    question: str | None = None


class DropReason(StrEnum):
    CLARIFICATION_REQUESTED = "clarification_requested"
    UNKNOWN_AGENT = "unknown_agent"
    DUPLICATE = "duplicate"
    TOO_MANY_STEPS = "too_many_steps"


@dataclass(frozen=True)
class DroppedStep:
    agent_id: str
    reason: DropReason


def find_drop_reason(
    step: Step, allowed_steps: list[Step]
) -> DropReason | None:
    """Return why the step can't join the allowed ones, or None if it can."""
    if step.agent_id not in AGENT_SPECS:
        return DropReason.UNKNOWN_AGENT
    if any(
        step.agent_id == allowed_step.agent_id
        and step.task == allowed_step.task
        for allowed_step in allowed_steps
    ):
        return DropReason.DUPLICATE
    if len(allowed_steps) >= MAX_PLAN_STEPS:
        return DropReason.TOO_MANY_STEPS
    return None


def restrict_access(step: Step) -> Step:
    """Return the step with write access removed unless its agent may write."""
    # Small models mark steps as writes generously, and a prompt can't fix that.
    if step.agent_id not in ACTION_AGENTS:
        return step.model_copy(update={"access_mode": AccessMode.READ})
    return step


def restrict_plan(plan: Plan) -> tuple[Plan, list[DroppedStep]]:
    """Return the plan code allows and the steps it removed."""
    question = (plan.question or "").strip()
    if question:
        # A clarifying question wins: nothing runs until the subscriber
        # answers it. The model asks and plans at once even when told not to,
        # so every step it added is dropped.
        dropped_steps = [
            DroppedStep(step.agent_id, DropReason.CLARIFICATION_REQUESTED)
            for step in plan.steps
        ]
        return plan.model_copy(
            update={"steps": [], "question": question}
        ), dropped_steps

    # No question: check the steps one by one, in the model's order. A step is
    # compared with the ones already allowed, so the first of two duplicates
    # stays and dropped steps don't count towards the step limit.
    allowed_steps: list[Step] = []
    dropped_steps = []
    for step in plan.steps:
        drop_reason = find_drop_reason(step, allowed_steps)
        if drop_reason is None:
            # The step will run, with write access only for an action agent.
            allowed_steps.append(restrict_access(step))
        else:
            dropped_steps.append(DroppedStep(step.agent_id, drop_reason))
    # A blank question is replaced with None: one value for "no question".
    return plan.model_copy(
        update={"steps": allowed_steps, "question": None}
    ), dropped_steps


def build_planner_agent() -> Agent:
    """Return the planner agent. It has no tools and returns a Plan."""
    return build_agent("planner", PLANNER_PROMPT, output_type=Plan)


async def propose_plan(
    planner: Agent, history: list[TResponseInputItem], message: str
) -> Plan:
    """Return the model's raw plan for the message"""
    run = await run_agent(
        planner, [*history, {"role": "user", "content": message}]
    )
    return run.output
