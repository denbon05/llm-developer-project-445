"""Unit tests run without services, so nothing here writes to a database."""

import pytest

from app import audit, telemetry


def do_nothing(*_args, **_kwargs) -> None:
    """Accept any arguments and do nothing."""


@pytest.fixture(autouse=True)
def skip_journal_writes(monkeypatch) -> None:
    """Make the audit and telemetry writers do nothing."""
    monkeypatch.setattr(audit, "append", do_nothing)
    monkeypatch.setattr(telemetry, "record_llm_call", do_nothing)
    monkeypatch.setattr(telemetry, "record_tool_call", do_nothing)
