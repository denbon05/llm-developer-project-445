"""One key runs its operation at most once, checked on the real table."""

import asyncio
import uuid
from collections.abc import Awaitable, Callable

import pytest

from app import idempotency
from core import config, db

ANNA_SUB = config.USERS["anna"].sub


class CountingOperation:
    """Returns the given result, or raises it, and counts its runs."""

    def __init__(self, result: dict | Exception) -> None:
        self.result = result
        self.run_count = 0

    async def __call__(self) -> dict:
        self.run_count += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.fixture
def idempotency_key() -> str:
    """Return a key no other test uses: the tests share one database."""
    return f"tariff_change:p_{uuid.uuid4().hex[:8]}"


def execute(
    idempotency_key: str, operation: Callable[[], Awaitable[dict]]
) -> idempotency.StoredResult:
    """Run the operation under the key as anna's tariff change."""
    return asyncio.run(
        idempotency.execute(
            idempotency_key, ANNA_SUB, "tariff_change", operation
        )
    )


def fetch_stored_row(idempotency_key: str) -> dict | None:
    """Return the row the table holds for the key."""
    return db.fetchone(
        "select sub, action, result from idempotency where key = %s",
        (idempotency_key,),
    )


def test_first_call_runs_the_operation_and_stores_its_result(idempotency_key):
    """A successful result is returned and kept under the key."""
    operation = CountingOperation({"outcome": "executed", "after": "Семейный"})

    stored_result = execute(idempotency_key, operation)

    assert operation.run_count == 1
    assert stored_result.is_duplicate is False
    assert stored_result.result == {"outcome": "executed", "after": "Семейный"}
    assert fetch_stored_row(idempotency_key) == {
        "sub": ANNA_SUB,
        "action": "tariff_change",
        "result": {"outcome": "executed", "after": "Семейный"},
    }


# The outcomes are listed by hand: built from ACCOUNT_CHANGED_OUTCOMES, the
# test would stop checking an outcome removed from it by mistake.
@pytest.mark.parametrize(
    "outcome", ["executed", "toggled", "updated", "duplicate"]
)
def test_retry_after_a_change_returns_the_stored_result_without_running(
    idempotency_key, outcome
):
    """The account already has the change, so the operation never runs again."""
    execute(idempotency_key, CountingOperation({"outcome": outcome}))
    retry_operation = CountingOperation({"outcome": "executed"})

    stored_result = execute(idempotency_key, retry_operation)

    assert retry_operation.run_count == 0
    assert stored_result.is_duplicate is True
    assert stored_result.result == {"outcome": outcome}


@pytest.mark.parametrize("outcome", ["daily_limit", "same_tariff", "not_found"])
def test_retry_after_a_refusal_runs_the_operation(idempotency_key, outcome):
    """A refusal changed nothing, so the retry is not answered as a repeat."""
    execute(idempotency_key, CountingOperation({"outcome": outcome}))
    retry_operation = CountingOperation({"outcome": "executed"})

    stored_result = execute(idempotency_key, retry_operation)

    assert retry_operation.run_count == 1
    assert stored_result.is_duplicate is False


def test_retry_after_a_failure_runs_the_operation(idempotency_key):
    """An exception stores nothing, so the retry runs the operation."""
    with pytest.raises(RuntimeError):
        execute(
            idempotency_key, CountingOperation(RuntimeError("server is down"))
        )
    retry_operation = CountingOperation({"outcome": "executed"})

    stored_result = execute(idempotency_key, retry_operation)

    assert retry_operation.run_count == 1
    assert stored_result.is_duplicate is False


def test_key_stored_by_the_billing_mock_does_not_fail_the_call(
    idempotency_key,
):
    """The change happened, so the call must not end in an error."""

    # The billing mock writes the same key to this table when it changes a
    # tariff, before idempotency.execute stores its own row.
    async def change_tariff_like_billing() -> dict:
        db.execute(
            "insert into idempotency(key, sub, action, result)"
            " values (%s, %s, 'tariff_change', '{}')",
            (idempotency_key, ANNA_SUB),
        )
        return {"outcome": "executed"}

    stored_result = execute(idempotency_key, change_tariff_like_billing)

    assert stored_result.result == {"outcome": "executed"}
