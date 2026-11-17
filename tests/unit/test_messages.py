"""Replies after execution are built by code. No services are needed."""

import pytest

from app import messages


def test_tariff_change_reply_names_both_tariffs_and_the_price():
    """The reply comes from the billing result, not from a model."""
    reply = messages.build_execution_reply(
        {
            "outcome": "executed",
            "before": "Базовый",
            "after": "Семейный",
            "price_before": 440,
            "price_after": 890,
        }
    )

    assert (
        reply == "Готово: тариф изменён с «Базовый» на «Семейный», 890 ₽/мес."
    )


@pytest.mark.parametrize(
    "result",
    [
        {"outcome": "toggled", "service": "Музыка", "enabled": False},
        {"outcome": "toggled", "service": "Роуминг", "enabled": True},
        {"outcome": "updated", "phone": "+7 900 111-22-33", "email": "a@b.ru"},
    ],
)
def test_every_success_starts_with_done(result):
    """Scenarios look for «Готово» after any successful change."""
    assert messages.build_execution_reply(result).startswith("Готово")


@pytest.mark.parametrize(
    ("enabled", "expected_reply"),
    [
        (True, "Готово: услуга «Музыка» подключена."),
        (False, "Готово: услуга «Музыка» отключена."),
    ],
)
def test_service_toggle_reply_says_on_or_off(enabled, expected_reply):
    """The reply names the state the service ended in."""
    reply = messages.build_execution_reply(
        {"outcome": "toggled", "service": "Музыка", "enabled": enabled}
    )

    assert reply == expected_reply


def test_duplicate_reply_is_exact():
    """A repeated "да" is told that nothing ran again."""
    assert (
        messages.build_execution_reply({"outcome": "duplicate"})
        == "Эта операция уже исполнена — повтор не выполнялся."
    )


def test_daily_limit_reply_offers_an_operator():
    """The limit is a business outcome with a way forward."""
    assert "оператор" in messages.build_execution_reply(
        {"outcome": "daily_limit"}
    )


def test_refusal_at_execution_is_readable():
    """A refusal from the mock becomes a sentence, not a code."""
    reply = messages.build_execution_reply({"outcome": "same_tariff"})

    assert reply == "Этот тариф уже подключён, менять нечего."


def test_unknown_outcome_offers_a_retry():
    """Anything unexpected is a failure the subscriber can retry with "да"."""
    assert (
        messages.build_execution_reply({"outcome": "?"})
        == messages.ACTION_FAILED
    )
