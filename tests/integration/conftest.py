"""A throwaway Postgres for the tests of this folder. Needs Docker."""

from collections.abc import Iterator

import pytest
from testcontainers.community.postgres import PostgresContainer

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
