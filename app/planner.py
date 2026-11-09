"""Planner: proposes which worker agents a turn runs, before anything executes.

The plan is LLM output, so execution doesn't trust it: gate() keeps only what
code allows. Changes to a tariff, service or contacts go through a confirmation
card and the permission policy, never straight from a plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from agents import Agent, TResponseInputItem
from pydantic import AliasChoices, BaseModel, Field, field_validator

from app.llm import build_agent, run_agent
from app.workers import CATALOG

READ_AGENTS = ("data", "kb")
# ACTION_AGENTS = ("tariff", "tickets")
MAX_PLAN_STEPS = 4

AGENT_LIST = "\n".join(
    f"- {agent_id}: {spec.description}" for agent_id, spec in CATALOG.items()
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


def find_drop_reason(step: Step, kept: list[Step]) -> DropReason | None:
    """Return why the step can't join the kept ones, or None if it can."""
    if step.agent_id not in CATALOG:
        return DropReason.UNKNOWN_AGENT
    if any(
        step.agent_id == other.agent_id and step.task == other.task
        for other in kept
    ):
        return DropReason.DUPLICATE
    if len(kept) >= MAX_PLAN_STEPS:
        return DropReason.TOO_MANY_STEPS
    return None


def restrict_access(step: Step) -> Step:
    """Return the step with write access removed from a read-only agent."""
    # Small models mark steps as writes generously, and a prompt can't fix that.
    if step.agent_id in READ_AGENTS:
        return step.model_copy(update={"access_mode": AccessMode.READ})
    return step


def gate(plan: Plan) -> tuple[Plan, list[DroppedStep]]:
    """Return the plan code allows and the steps it removed."""
    question = (plan.question or "").strip()
    if question:
        # The model asks and plans at once even when told not to.
        dropped = [
            DroppedStep(step.agent_id, DropReason.CLARIFICATION_REQUESTED)
            for step in plan.steps
        ]
        return plan.model_copy(
            update={"steps": [], "question": question}
        ), dropped

    kept: list[Step] = []
    dropped = []
    for step in plan.steps:
        reason = find_drop_reason(step, kept)
        if reason is None:
            kept.append(restrict_access(step))
        else:
            dropped.append(DroppedStep(step.agent_id, reason))
    return plan.model_copy(update={"steps": kept, "question": None}), dropped


def build_planner_agent() -> Agent:
    """Return the planner agent. It has no tools and returns a Plan."""
    return build_agent("planner", PLANNER_PROMPT, output_type=Plan)


async def propose_plan(
    planner: Agent, history: list[TResponseInputItem], message: str
) -> Plan:
    """Return the model's plan for this message, before the gate."""
    run = await run_agent(
        planner, [*history, {"role": "user", "content": message}]
    )
    return run.output
