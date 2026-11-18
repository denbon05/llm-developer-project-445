"""Idempotent execution: one key runs its operation at most once."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from actions.contracts import Outcome
from core import db

# Outcomes after which the account has the change. Their result is stored under
# the key, so the operation never runs with that key again. `duplicate` is the
# billing mock saying it made this change earlier with the same key. Any other
# outcome (daily_limit, same_tariff, not_found, ...) changed nothing and is not
# stored, so a later retry with the same key runs the operation again.
ACCOUNT_CHANGED_OUTCOMES = frozenset(
    {Outcome.EXECUTED, Outcome.TOGGLED, Outcome.UPDATED, Outcome.DUPLICATE}
)


@dataclass(frozen=True)
class StoredResult:
    """The result stored under an idempotency key."""

    is_duplicate: bool  # the key was taken before, so the operation didn't run
    result: dict


async def execute(
    key: str, sub: str, action: str, operation: Callable[[], Awaitable[dict]]
) -> StoredResult:
    """Run the operation unless its key already has a stored result."""
    stored_row = db.fetchone(
        """
        select result
        from idempotency
        where key = %s
        """,
        (key,),
    )
    if stored_row is not None:
        return StoredResult(
            is_duplicate=True, result=stored_row["result"] or {}
        )
    # The check above and the insert below are not atomic: the caller must
    # not run two calls with one key at the same time.
    result = await operation()
    if result.get("outcome") in ACCOUNT_CHANGED_OUTCOMES:
        # The billing mock inserts the same key into this table when it changes
        # a tariff, so the row may exist already: keep the first one.
        db.execute(
            """
            insert into idempotency (key, sub, action, result)
            values (%s, %s, %s, %s)
            on conflict (key) do nothing
            """,
            (key, sub, action, json.dumps(result, ensure_ascii=False)),
        )
    return StoredResult(is_duplicate=False, result=result)
