"""Action contracts: normalization and refusal data. No services are needed."""

import pytest

from actions.contracts import (
    ActionError,
    ErrorCode,
    normalize_email,
    normalize_phone,
    normalize_service,
    normalize_tariff,
)

TARIFFS = [
    {"name": "Базовый", "price_rub": 440, "available": True},
    {"name": "Семейный", "price_rub": 890, "available": True},
    {"name": "Про", "price_rub": 350, "available": False},
]
SERVICES = ["Музыка", "Роуминг"]


@pytest.mark.parametrize(
    "spoken_name",
    ["семейный", "Семейный", " «Семейный» ", "тариф Семейный", '"СЕМЕЙНЫЙ"'],
)
def test_tariff_name_as_the_model_heard_it(spoken_name):
    """Case, quotes and the word «тариф» don't change the tariff."""
    assert normalize_tariff(spoken_name, TARIFFS) == "Семейный"


def test_unknown_tariff_lists_only_available_ones():
    """The hint offers tariffs the subscriber can actually switch to."""
    with pytest.raises(ActionError) as error_info:
        normalize_tariff("люкс", TARIFFS)

    error_data = error_info.value.to_mcp_data()
    assert error_data["error"] == ErrorCode.UNKNOWN_TARIFF
    assert error_data["available"] == ["Базовый", "Семейный"]
    assert error_data["retryable"] is True


def test_no_prefix_matching():
    """A partial name is unknown: a guess could change the wrong tariff."""
    with pytest.raises(ActionError) as error_info:
        normalize_tariff("семей", TARIFFS)

    assert error_info.value.code == ErrorCode.UNKNOWN_TARIFF


def test_closed_tariff_is_a_business_rule():
    """Asking again for a closed tariff can't help."""
    with pytest.raises(ActionError) as error_info:
        normalize_tariff("про", TARIFFS)

    error_data = error_info.value.to_mcp_data()
    assert error_data["error"] == ErrorCode.TARIFF_NOT_AVAILABLE
    assert error_data["retryable"] is False


def test_service_name_ignores_the_word_service():
    """«услуга», case and ё/е don't change the service."""
    service_catalog = [*SERVICES, "Чёрный список"]

    assert normalize_service("услуга «музыка»", service_catalog) == "Музыка"
    assert normalize_service("РОУМИНГ", service_catalog) == "Роуминг"
    assert (
        normalize_service("черный список", service_catalog) == "Чёрный список"
    )


def test_unknown_service_lists_the_catalog():
    """The hint names every service the model may pass instead."""
    with pytest.raises(ActionError) as error_info:
        normalize_service("кино", SERVICES)

    assert error_info.value.to_mcp_data()["available"] == SERVICES


@pytest.mark.parametrize(
    "spoken_phone",
    ["+7 900 111-22-33", "89001112233", "8 (900) 111 22 33", "7.900.111.22.33"],
)
def test_phone_in_the_stand_format(spoken_phone):
    """Any separators and a leading 8 or 7 give one format."""
    assert normalize_phone(spoken_phone) == "+7 900 111-22-33"


@pytest.mark.parametrize(
    "spoken_phone",
    ["900 111-22-33", "+1 900 111 22 33", "+7 900 111-22-33-4", "семь"],
)
def test_bad_phone_gives_an_example(spoken_phone):
    """The model learns the expected shape from the example."""
    with pytest.raises(ActionError) as error_info:
        normalize_phone(spoken_phone)

    error_data = error_info.value.to_mcp_data()
    assert error_data["error"] == ErrorCode.BAD_PHONE
    assert error_data["example"]


def test_email_is_trimmed_and_lowercased():
    """The stored address doesn't depend on how it was typed."""
    assert normalize_email("  Anna@Example.RU ") == "anna@example.ru"


@pytest.mark.parametrize(
    "spoken_email", ["anna", "anna@", "anna@example", "a b@c.ru"]
)
def test_bad_email_gives_an_example(spoken_email):
    """Something that isn't an address is refused with an example."""
    with pytest.raises(ActionError) as error_info:
        normalize_email(spoken_email)

    error_data = error_info.value.to_mcp_data()
    assert error_data["error"] == ErrorCode.BAD_EMAIL
    assert error_data["example"]


def test_error_data_carries_code_detail_and_hints():
    """Everything the model needs is in one flat dict."""
    error = ActionError(ErrorCode.SAME_CONTACTS, "уже указаны", field="email")

    assert error.to_mcp_data() == {
        "error": "same_contacts",
        "detail": "уже указаны",
        "retryable": False,
        "field": "email",
    }
