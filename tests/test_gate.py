"""The gate decides what a plan may run. No services or model are needed."""

import pytest
from pydantic import ValidationError

from app.planner import (
    MAX_PLAN_STEPS,
    AccessMode,
    DroppedStep,
    DropReason,
    Plan,
    Step,
    gate,
)


def make_plan(*steps: tuple[str, str], question: str | None = None) -> Plan:
    """Return a plan as the model could have written it."""
    return Plan(
        reasoning="test",
        steps=[Step(agent_id=agent_id, task=task) for agent_id, task in steps],
        question=question,
    )


def test_keeps_known_agents():
    """A valid plan passes through unchanged."""
    plan, dropped = gate(make_plan(("data", "tariff"), ("kb", "rules")))

    assert [step.agent_id for step in plan.steps] == ["data", "kb"]
    assert dropped == []


def test_drops_unknown_agent_with_reason():
    """An agent the model invented never runs, and the reason is kept."""
    plan, dropped = gate(make_plan(("billing", "check"), ("data", "tariff")))

    assert [step.agent_id for step in plan.steps] == ["data"]
    assert dropped == [DroppedStep("billing", DropReason.UNKNOWN_AGENT)]


def test_read_agent_loses_write_access():
    """Small models mark steps as writes generously, so code corrects it."""
    model_plan = make_plan(("kb", "rules"))
    model_plan.steps[0].access_mode = AccessMode.WRITE

    plan, _ = gate(model_plan)

    assert plan.steps[0].access_mode is AccessMode.READ


def test_drops_exact_duplicate_only():
    """The same agent may run twice, but only for different tasks."""
    plan, dropped = gate(
        make_plan(("data", "tariff"), ("data", "tariff"), ("data", "charges"))
    )

    assert [step.task for step in plan.steps] == ["tariff", "charges"]
    assert dropped == [DroppedStep("data", DropReason.DUPLICATE)]


def test_keeps_at_most_the_step_limit():
    """Every step is a separate agent run, so a plan can't be endless."""
    steps = [("data", f"task {number}") for number in range(MAX_PLAN_STEPS + 1)]

    plan, dropped = gate(make_plan(*steps))

    assert len(plan.steps) == MAX_PLAN_STEPS
    assert dropped == [DroppedStep("data", DropReason.TOO_MANY_STEPS)]


def test_duplicates_do_not_use_up_the_step_limit():
    """Duplicates are removed first, so useful steps aren't cut instead."""
    steps = [("data", "same")] * 2 + [
        ("data", f"task {number}") for number in range(MAX_PLAN_STEPS - 1)
    ]

    plan, dropped = gate(make_plan(*steps))

    assert len(plan.steps) == MAX_PLAN_STEPS
    assert dropped == [DroppedStep("data", DropReason.DUPLICATE)]


def test_question_wins_over_steps():
    """A model that asks and plans at once needs an answer before it acts."""
    plan, dropped = gate(
        make_plan(("kb", "guess"), question=" Что случилось? ")
    )

    assert plan.steps == []
    assert plan.question == "Что случилось?"
    assert dropped == [
        DroppedStep("kb", DropReason.CLARIFICATION_REQUESTED),
    ]


def test_blank_question_is_no_question():
    """Whitespace in the question field must not cancel a valid plan."""
    plan, _ = gate(make_plan(("data", "tariff"), question="  "))

    assert plan.question is None
    assert len(plan.steps) == 1


@pytest.mark.parametrize(
    "fields",
    [
        {"agent_id": "data", "task": "x"},
        {"agent": "data", "task": "x"},
        {"agentName": "data", "job": "x"},
    ],
)
def test_step_accepts_model_dialects(fields):
    """Small models don't always use the field names from the schema."""
    step = Step.model_validate(fields)

    assert (step.agent_id, step.task) == ("data", "x")


def test_unknown_access_mode_becomes_read():
    """Nothing runs because of this field, so it must not fail the turn."""
    step = Step.model_validate(
        {"agent_id": "data", "task": "x", "access_mode": "maybe"}
    )

    assert step.access_mode is AccessMode.READ


def test_plan_in_another_shape_is_an_error():
    """A reply in the model's own shape fails instead of becoming empty.

    With an optional `steps`, Pydantic ignored the unknown `plan` key and the
    turn silently ran with no agents.
    """
    with pytest.raises(ValidationError):
        Plan.model_validate({"plan": [["data", "x"]]})
