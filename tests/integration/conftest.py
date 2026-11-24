"""A throwaway Postgres and a fake guardrail for the tests of this folder.

Needs Docker.
"""

from collections.abc import Iterator

import pytest
from testcontainers.community.postgres import PostgresContainer

from app import guardrail_client
from app.guardrail_client import Verdict
from core import db

# The image docker-compose.yml uses: the schema needs the pgvector extension.
POSTGRES_IMAGE = "pgvector/pgvector:pg16"


@pytest.fixture(scope="session", autouse=True)
def postgres() -> Iterator[None]:
    """Start Postgres, apply the schema and point core.db at it."""
    # driver=None gives a plain postgresql:// address, which psycopg reads.
    with PostgresContainer(POSTGRES_IMAGE, driver=None) as container:
        dsn_patch = pytest.MonkeyPatch()
        dsn_patch.setattr(db, "PG_DSN", container.get_connection_url())
        db.migrate()
        yield
        dsn_patch.undo()


@pytest.fixture(autouse=True)
def empty_tables() -> None:
    """Start every test with empty tables, whatever the last test left."""
    # pytest sets up session fixtures first, so Postgres is already running.
    # Code under test opens its own connections and commits, so a test can't
    # roll its changes back; emptying the tables isolates the tests instead.
    db.execute(
        """
        truncate audit_log, llm_calls, tool_calls, policy_events,
            pending_cards, idempotency
        restart identity
        """
    )


class FakeGuardrail:
    """Answers every check safe unless a test sets a verdict for its mode."""

    def __init__(self) -> None:
        # The mode, such as "input", and the verdict every check in it gets.
        self.verdict_by_mode: dict[str, Verdict] = {}

    async def check(self, text: str, mode: str) -> Verdict:
        return self.verdict_by_mode.get(
            mode, Verdict(safe=True, category=None, available=True)
        )


@pytest.fixture(autouse=True)
def fake_guardrail(monkeypatch) -> FakeGuardrail:
    """Replace the guardrail client's check with a fake's."""
    guardrail = FakeGuardrail()
    monkeypatch.setattr(guardrail_client, "check", guardrail.check)
    return guardrail
