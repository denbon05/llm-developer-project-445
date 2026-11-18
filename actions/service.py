"""Actions on a subscriber's account, carried out through the REST mocks."""

from __future__ import annotations

import json

import httpx2

from actions.contracts import (
    ActionError,
    ActionKind,
    ErrorCode,
    Outcome,
    normalize_email,
    normalize_phone,
    normalize_service,
    normalize_tariff,
)
from core import config

MOCK_TIMEOUT_SEC = 15


async def request_mock(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
) -> dict:
    """Send one request to a REST mock and return its JSON body."""
    # trust_env=False: a system proxy must not intercept calls to 127.0.0.1.
    async with httpx2.AsyncClient(
        base_url=config.STAND_URL, trust_env=False, timeout=MOCK_TIMEOUT_SEC
    ) as http:
        response = await http.request(method, path, params=params, json=body)
    response.raise_for_status()
    return response.json()


async def fetch_subscriber(sub: str) -> dict:
    """Return the subscriber's profile, tariff and services from the CRM."""
    return await request_mock(
        "GET", "/mock/crm/subscriber", params={"sub": sub}
    )


def build_prepared_action(
    action: ActionKind, details: dict, view: dict
) -> dict:
    """Return a prepared action: what to execute and what the card shows."""
    return {
        "ok": True,
        "action": str(action),
        "details": details,
        "view": view,
    }


def format_price(price_rub: int) -> str:
    """Return a monthly price as text."""
    # I18N: the currency and the period.
    return f"{price_rub} ₽/мес"


async def prepare_tariff_change(sub: str, tariff: str) -> dict:
    """Check a tariff change and describe it for a confirmation card."""
    subscriber = await fetch_subscriber(sub)
    tariff_catalog = (await request_mock("GET", "/mock/crm/tariffs"))["tariffs"]
    new_tariff = normalize_tariff(tariff, tariff_catalog)
    current_tariff = subscriber["tariff"]
    if new_tariff == current_tariff:
        raise ActionError(
            ErrorCode.SAME_TARIFF,
            f"Тариф «{current_tariff}» уже подключён.",
            current=current_tariff,
        )
    price_by_tariff = {
        catalog_tariff["name"]: catalog_tariff["price_rub"]
        for catalog_tariff in tariff_catalog
    }
    current_price = price_by_tariff[current_tariff]
    new_price = price_by_tariff[new_tariff]
    # I18N: card labels. The web chat shows the keys as row labels.
    view = {
        "Сейчас": f"{current_tariff}, {format_price(current_price)}",
        "Станет": f"{new_tariff}, {format_price(new_price)}",
        "Разница": f"{new_price - current_price:+d} ₽/мес",
    }
    return build_prepared_action(
        ActionKind.TARIFF_CHANGE, {"tariff": new_tariff}, view
    )


async def prepare_toggle_service(
    sub: str, service: str, should_enable: bool
) -> dict:
    """Check turning a service on or off and describe it for a card."""
    subscriber = await fetch_subscriber(sub)
    # The CRM mock toggles only the services listed for the subscriber, so
    # that list is the catalog.
    service_by_name = {
        service_row["service"]: service_row
        for service_row in subscriber["services"]
    }
    service_name = normalize_service(service, list(service_by_name))
    is_enabled_now = service_by_name[service_name]["enabled"]
    if is_enabled_now and should_enable:
        raise ActionError(
            ErrorCode.SERVICE_ALREADY_ON,
            f"Услуга «{service_name}» уже подключена.",
        )
    if not is_enabled_now and not should_enable:
        raise ActionError(
            ErrorCode.SERVICE_ALREADY_OFF,
            f"Услуга «{service_name}» уже отключена.",
        )
    price = format_price(service_by_name[service_name]["price_rub"])
    # I18N: card labels and service states.
    view = {
        "Услуга": f"{service_name}, {price}",
        "Сейчас": "отключена" if should_enable else "подключена",
        "Станет": "подключена" if should_enable else "отключена",
    }
    return build_prepared_action(
        ActionKind.SERVICE_TOGGLE,
        {"service": service_name, "enabled": should_enable},
        view,
    )


def nullify_blank(value: str | None) -> str | None:
    """Return None for a null or blank value, and any other value unchanged."""
    return value if value and value.strip() else None


async def prepare_contacts_update(
    sub: str, phone: str | None, email: str | None
) -> dict:
    """Check new contact details and describe the change for a card."""
    phone = nullify_blank(phone)
    email = nullify_blank(email)
    if phone is None and email is None:
        raise ActionError(
            ErrorCode.NOTHING_TO_UPDATE, "Нужен новый телефон или почта."
        )
    new_contacts = {
        "phone": normalize_phone(phone) if phone is not None else None,
        "email": normalize_email(email) if email is not None else None,
    }
    subscriber = await fetch_subscriber(sub)
    # Only the profile fields that really change go to the card and to execution.
    details = {
        field: new_value
        for field, new_value in new_contacts.items()
        if new_value is not None and new_value != subscriber[field]
    }
    if not details:
        raise ActionError(
            ErrorCode.SAME_CONTACTS, "Эти контакты уже указаны в профиле."
        )
    # I18N: card labels.
    field_labels = {"phone": "Телефон", "email": "Почта"}
    view = {
        field_labels[field]: f"{subscriber[field]} → {new_value}"
        for field, new_value in details.items()
    }
    return build_prepared_action(ActionKind.CONTACTS_UPDATE, details, view)


def parse_details(details_json: str) -> dict:
    """Return the card details passed to execution as a JSON string."""
    try:
        details = json.loads(details_json)
    except ValueError:
        details = None
    if not isinstance(details, dict):
        raise ActionError(
            ErrorCode.BAD_DETAILS, "details_json должен быть JSON-объектом."
        )
    return details


async def execute_tariff_change(
    sub: str, details: dict, idempotency_key: str
) -> dict:
    """Change the tariff in the billing; its answer has `before` and `after`."""
    # The billing mock keeps its own idempotency record and daily limit, so a
    # retried key never changes the tariff twice.
    return await request_mock(
        "POST",
        "/mock/billing/tariff-change",
        body={
            "sub": sub,
            "tariff": details.get("tariff", ""),
            "idempotency_key": idempotency_key,
        },
    )


async def execute_service_toggle(sub: str, details: dict) -> dict:
    """Turn a service on or off; a change also reports `before` and `after`."""
    service_name = details.get("service", "")
    # The CRM's answer has only the new state, so the old one is read first.
    subscriber = await fetch_subscriber(sub)
    toggle_result = await request_mock(
        "POST",
        "/mock/crm/toggle-service",
        body={
            "sub": sub,
            "service": service_name,
            "enabled": details.get("enabled", True),
        },
    )
    if toggle_result.get("outcome") != Outcome.TOGGLED:
        return toggle_result
    enabled_by_service = {
        service_row["service"]: service_row["enabled"]
        for service_row in subscriber["services"]
    }
    return {
        **toggle_result,
        "before": enabled_by_service.get(service_name),
        "after": toggle_result["enabled"],
    }


async def execute_contacts_update(sub: str, details: dict) -> dict:
    """Set new contacts; a change also reports the fields before and after."""
    new_contacts = {
        "phone": details.get("phone"),
        "email": details.get("email"),
    }
    # The CRM's answer has only the new contacts, so the old ones are read
    # first.
    subscriber = await fetch_subscriber(sub)
    update_result = await request_mock(
        "POST", "/mock/crm/contacts", body={"sub": sub, **new_contacts}
    )
    if update_result.get("outcome") != Outcome.UPDATED:
        return update_result
    # The CRM keeps a contact sent as null, so only the others change.
    changed_fields = [
        field
        for field, new_value in new_contacts.items()
        if new_value is not None
    ]
    return {
        **update_result,
        "before": {field: subscriber[field] for field in changed_fields},
        "after": {field: update_result[field] for field in changed_fields},
    }


async def execute_action(
    sub: str, action: str, details_json: str, idempotency_key: str
) -> dict:
    """Execute a confirmed action and return the mock's outcome."""
    details = parse_details(details_json)
    # Preconditions are not checked again: the mocks refuse what they can't
    # do, and setting a service or contact to its current value is harmless.
    match action:
        case ActionKind.TARIFF_CHANGE:
            return await execute_tariff_change(sub, details, idempotency_key)
        case ActionKind.SERVICE_TOGGLE:
            return await execute_service_toggle(sub, details)
        case ActionKind.CONTACTS_UPDATE:
            return await execute_contacts_update(sub, details)
    raise ActionError(
        ErrorCode.UNKNOWN_ACTION,
        f"Неизвестное действие «{action}».",
        available=[str(kind) for kind in ActionKind],
    )


async def create_ticket(sub: str, subject: str, note: str) -> dict:
    """Open a support ticket for a human operator."""
    return await request_mock(
        "POST",
        "/mock/tickets/create",
        body={"sub": sub, "subject": subject, "note": note},
    )
