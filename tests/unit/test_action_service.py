"""Preparing and executing actions. A stub replaces the REST mocks."""

import asyncio

import pytest

from actions import service
from actions.contracts import ActionError, ErrorCode
from core import config

ANNA_SUB = config.USERS["anna"].sub

SUBSCRIBER = {
    "tariff": "Базовый",
    "phone": "+7 900 111-22-33",
    "email": "anna@example.ru",
    "services": [
        {"service": "Музыка", "enabled": True, "price_rub": 150},
        {"service": "Роуминг", "enabled": False, "price_rub": 300},
    ],
}
TARIFF_CATALOG = [
    {"name": "Базовый", "price_rub": 440, "available": True},
    {"name": "Семейный", "price_rub": 890, "available": True},
    {"name": "Эконом", "price_rub": 290, "available": True},
]


class StubRestMocks:
    """Answers like the REST mocks and remembers the bodies sent to them."""

    def __init__(self) -> None:
        self.response_by_path = {
            "/mock/crm/subscriber": SUBSCRIBER,
            "/mock/crm/tariffs": {"tariffs": TARIFF_CATALOG},
        }
        self.sent_bodies: list[tuple[str, dict]] = []

    async def request(
        self,
        _method: str,
        path: str,
        *,
        params: dict | None = None,
        body: dict | None = None,
    ) -> dict:
        if body is not None:
            self.sent_bodies.append((path, body))
        # A path without a prepared response answers like a successful write.
        return self.response_by_path.get(path, {"outcome": "ok"})


# autouse: every test of this module gets the stub, so none of them can reach
# the stand's REST mocks and change its data. A test that checks what was sent
# takes stub_rest_mocks as an argument.
@pytest.fixture(autouse=True)
def stub_rest_mocks(monkeypatch) -> StubRestMocks:
    """Replace the REST mocks behind actions.service with a stub."""
    stub_rest_mocks = StubRestMocks()
    monkeypatch.setattr(service, "request_mock", stub_rest_mocks.request)
    return stub_rest_mocks


def refusal_code(prepare_call) -> ErrorCode:
    """Return the code the call is refused with."""
    with pytest.raises(ActionError) as error_info:
        asyncio.run(prepare_call)
    return error_info.value.code


@pytest.mark.parametrize(
    ("spoken_tariff", "expected_view"),
    [
        (
            "семейный",
            {
                "Сейчас": "Базовый, 440 ₽/мес",
                "Станет": "Семейный, 890 ₽/мес",
                "Разница": "+450 ₽/мес",
            },
        ),
        (
            "эконом",
            {
                "Сейчас": "Базовый, 440 ₽/мес",
                "Станет": "Эконом, 290 ₽/мес",
                "Разница": "-150 ₽/мес",
            },
        ),
    ],
)
def test_tariff_card_shows_both_prices_and_the_difference(
    spoken_tariff, expected_view
):
    """The difference carries its sign, so a cheaper tariff reads as a minus."""
    prepared_action = asyncio.run(
        service.prepare_tariff_change(ANNA_SUB, spoken_tariff)
    )

    assert prepared_action["ok"] is True
    assert prepared_action["action"] == "tariff_change"
    assert prepared_action["view"] == expected_view


def test_tariff_card_executes_the_catalog_name():
    """Execution gets the tariff as the catalog spells it."""
    prepared_action = asyncio.run(
        service.prepare_tariff_change(ANNA_SUB, "тариф «семейный»")
    )

    assert prepared_action["details"] == {"tariff": "Семейный"}


def test_current_tariff_is_refused():
    """Switching to the tariff the subscriber already has makes no card."""
    code = refusal_code(service.prepare_tariff_change(ANNA_SUB, "базовый"))

    assert code == ErrorCode.SAME_TARIFF


@pytest.mark.parametrize(
    ("spoken_service", "should_enable", "expected_view"),
    [
        (
            "роуминг",
            True,
            {
                "Услуга": "Роуминг, 300 ₽/мес",
                "Сейчас": "отключена",
                "Станет": "подключена",
            },
        ),
        (
            "музыка",
            False,
            {
                "Услуга": "Музыка, 150 ₽/мес",
                "Сейчас": "подключена",
                "Станет": "отключена",
            },
        ),
    ],
)
def test_service_card_shows_the_state_before_and_after(
    spoken_service, should_enable, expected_view
):
    """The card names the service, its price and both states."""
    prepared_action = asyncio.run(
        service.prepare_toggle_service(ANNA_SUB, spoken_service, should_enable)
    )

    assert prepared_action["action"] == "service_toggle"
    assert prepared_action["view"] == expected_view


@pytest.mark.parametrize(
    ("spoken_service", "should_enable", "expected_code"),
    [
        ("музыка", True, ErrorCode.SERVICE_ALREADY_ON),
        ("роуминг", False, ErrorCode.SERVICE_ALREADY_OFF),
    ],
)
def test_service_already_in_the_wanted_state_is_refused(
    spoken_service, should_enable, expected_code
):
    """There is nothing to confirm when the toggle changes nothing."""
    code = refusal_code(
        service.prepare_toggle_service(ANNA_SUB, spoken_service, should_enable)
    )

    assert code == expected_code


def test_service_card_executes_the_catalog_name_and_the_new_state():
    """Execution gets the service as the CRM spells it."""
    prepared_action = asyncio.run(
        service.prepare_toggle_service(ANNA_SUB, "услуга роуминг", True)
    )

    assert prepared_action["details"] == {"service": "Роуминг", "enabled": True}


def test_service_the_subscriber_does_not_have_is_unknown():
    """The subscriber's own service list is the catalog."""
    with pytest.raises(ActionError) as error_info:
        asyncio.run(service.prepare_toggle_service(ANNA_SUB, "антивирус", True))

    error_data = error_info.value.to_mcp_data()
    assert error_data["error"] == ErrorCode.UNKNOWN_SERVICE
    assert error_data["available"] == ["Музыка", "Роуминг"]


def test_contacts_card_holds_only_the_contact_that_changes():
    """A contact equal to the current one stays out of the card."""
    # The phone is the current one, written in another format.
    prepared_action = asyncio.run(
        service.prepare_contacts_update(
            ANNA_SUB, "89001112233", "new@example.ru"
        )
    )

    assert prepared_action["action"] == "contacts_update"
    assert prepared_action["details"] == {"email": "new@example.ru"}
    assert prepared_action["view"] == {
        "Почта": "anna@example.ru → new@example.ru"
    }


def test_contacts_card_holds_both_contacts_when_both_change():
    """Each changed contact gets its own row with the old and new value."""
    prepared_action = asyncio.run(
        service.prepare_contacts_update(
            ANNA_SUB, "8 900 555-44-33", "new@example.ru"
        )
    )

    assert prepared_action["view"] == {
        "Телефон": "+7 900 111-22-33 → +7 900 555-44-33",
        "Почта": "anna@example.ru → new@example.ru",
    }


def test_contacts_equal_to_the_current_ones_are_refused():
    """Nothing would change, so there is no card."""
    code = refusal_code(
        service.prepare_contacts_update(
            ANNA_SUB, "+7 900 111-22-33", "Anna@Example.ru"
        )
    )

    assert code == ErrorCode.SAME_CONTACTS


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("", None), ("  ", None), ("a@b.ru", "a@b.ru")],
)
def test_null_and_blank_mean_not_given(value, expected):
    """Only a real value counts as a new contact."""
    assert service.nullify_blank(value) == expected


def test_tariff_change_goes_to_billing_with_the_idempotency_key(
    stub_rest_mocks,
):
    """Billing needs the key to refuse a second change itself."""
    asyncio.run(
        service.execute_action(
            ANNA_SUB, "tariff_change", '{"tariff": "Семейный"}', "key_1"
        )
    )

    assert stub_rest_mocks.sent_bodies == [
        (
            "/mock/billing/tariff-change",
            {"sub": ANNA_SUB, "tariff": "Семейный", "idempotency_key": "key_1"},
        )
    ]


def test_service_toggle_goes_to_the_crm(stub_rest_mocks):
    """The CRM gets the service and the state to set; both states come back."""
    stub_rest_mocks.response_by_path["/mock/crm/toggle-service"] = {
        "outcome": "toggled",
        "service": "Роуминг",
        "enabled": True,
    }

    execution_result = asyncio.run(
        service.execute_action(
            ANNA_SUB,
            "service_toggle",
            '{"service": "Роуминг", "enabled": true}',
            "key_1",
        )
    )

    assert stub_rest_mocks.sent_bodies == [
        (
            "/mock/crm/toggle-service",
            {"sub": ANNA_SUB, "service": "Роуминг", "enabled": True},
        )
    ]
    # Роуминг was off in the CRM before the call.
    assert execution_result.get("before") is False
    assert execution_result.get("after") is True


def test_contacts_update_sends_null_for_the_contact_it_keeps(stub_rest_mocks):
    """The CRM keeps a contact that arrives as null; only the other changes."""
    stub_rest_mocks.response_by_path["/mock/crm/contacts"] = {
        "outcome": "updated",
        "phone": "+7 900 111-22-33",
        "email": "new@example.ru",
    }

    execution_result = asyncio.run(
        service.execute_action(
            ANNA_SUB, "contacts_update", '{"email": "new@example.ru"}', "key_1"
        )
    )

    assert stub_rest_mocks.sent_bodies == [
        (
            "/mock/crm/contacts",
            {"sub": ANNA_SUB, "phone": None, "email": "new@example.ru"},
        )
    ]
    assert execution_result.get("before") == {"email": "anna@example.ru"}
    assert execution_result.get("after") == {"email": "new@example.ru"}


def test_unknown_action_is_refused_with_the_known_ones(stub_rest_mocks):
    """Nothing is sent, and the refusal lists what can be executed."""
    with pytest.raises(ActionError) as error_info:
        asyncio.run(service.execute_action(ANNA_SUB, "delete", "{}", "key_1"))

    error_data = error_info.value.to_mcp_data()
    assert error_data["error"] == ErrorCode.UNKNOWN_ACTION
    assert error_data["available"] == [
        "tariff_change",
        "service_toggle",
        "contacts_update",
    ]
    assert stub_rest_mocks.sent_bodies == []


@pytest.mark.parametrize("details_json", ["not json", "[1, 2]", "null"])
def test_details_that_are_not_a_json_object_are_refused(
    stub_rest_mocks, details_json
):
    """Execution needs the card details as an object."""
    code = refusal_code(
        service.execute_action(ANNA_SUB, "tariff_change", details_json, "key_1")
    )

    assert code == ErrorCode.BAD_DETAILS
    assert stub_rest_mocks.sent_bodies == []


def test_ticket_goes_to_the_ticket_mock(stub_rest_mocks):
    """The ticket carries the subscriber, the subject and the note."""
    asyncio.run(service.create_ticket(ANNA_SUB, "Смена тарифа", "не работает"))

    assert stub_rest_mocks.sent_bodies == [
        (
            "/mock/tickets/create",
            {"sub": ANNA_SUB, "subject": "Смена тарифа", "note": "не работает"},
        )
    ]
