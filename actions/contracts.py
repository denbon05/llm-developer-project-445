"""Action contracts: error codes, execution outcomes and input normalization."""

from __future__ import annotations

import re
from enum import IntEnum, StrEnum


class McpErrorCode(IntEnum):
    """JSON-RPC error codes of the action server."""

    # JSON-RPC leaves -32000..-32099 to the server. The stand's read servers
    # use -32003 for a role refusal, so an action refusal gets its own code.
    ACTION_REFUSED = -32002


class ErrorCode(StrEnum):
    """Why an action call was refused."""

    # Validation: the model can fix the call using the hints.
    UNKNOWN_TARIFF = "unknown_tariff"
    UNKNOWN_SERVICE = "unknown_service"
    BAD_PHONE = "bad_phone"
    BAD_EMAIL = "bad_email"
    NOTHING_TO_UPDATE = "nothing_to_update"
    UNKNOWN_ACTION = "unknown_action"
    BAD_DETAILS = "bad_details"
    # Business rules: the same call would be refused again.
    SAME_TARIFF = "same_tariff"
    TARIFF_NOT_AVAILABLE = "tariff_not_available"
    SERVICE_ALREADY_ON = "service_already_on"
    SERVICE_ALREADY_OFF = "service_already_off"
    SAME_CONTACTS = "same_contacts"


BUSINESS_RULE_CODES = frozenset(
    {
        ErrorCode.SAME_TARIFF,
        ErrorCode.TARIFF_NOT_AVAILABLE,
        ErrorCode.SERVICE_ALREADY_ON,
        ErrorCode.SERVICE_ALREADY_OFF,
        ErrorCode.SAME_CONTACTS,
    }
)


class ActionKind(StrEnum):
    """The actions a subscriber can confirm."""

    TARIFF_CHANGE = "tariff_change"
    SERVICE_TOGGLE = "service_toggle"
    CONTACTS_UPDATE = "contacts_update"


class Outcome(StrEnum):
    """What execute_action reports. The REST mocks decide every value."""

    EXECUTED = "executed"
    TOGGLED = "toggled"
    UPDATED = "updated"
    DUPLICATE = "duplicate"
    DAILY_LIMIT = "daily_limit"
    NOT_FOUND = "not_found"
    SAME_TARIFF = "same_tariff"
    UNKNOWN_TARIFF = "unknown_tariff"
    TARIFF_NOT_AVAILABLE = "tariff_not_available"
    UNKNOWN_SERVICE = "unknown_service"


class ActionError(Exception):
    """A refused action call: a machine-readable code, a message and hints."""

    def __init__(self, code: ErrorCode, message: str, **hints: object) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.hints = hints

    def to_mcp_data(self) -> dict:
        """Return the refusal as data the model can act on."""
        return {
            "error": str(self.code),
            "detail": self.message,
            # Tells the model whether fixing the arguments can help.
            "retryable": self.code not in BUSINESS_RULE_CODES,
            **self.hints,
        }


# Quote marks the model may keep around a name: straight, curly, guillemets.
QUOTE_MARKS = re.compile(r"[\"'«»„“”‘’]")
PHONE_CHARACTERS = re.compile(r"\+?[\d\s()\-.]+")
EMAIL_SHAPE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
PHONE_DIGITS = 11
# I18N: the example matches the stand's phone format.
PHONE_EXAMPLE = "+7 900 111-22-33"
EMAIL_EXAMPLE = "name@example.ru"


def clean_name(name: str, kind_word: str) -> str:
    """Return a name in a comparable form, without quotes and its kind word."""
    # I18N: ё/е are one letter in Russian names.
    cleaned_name = QUOTE_MARKS.sub("", name).casefold().replace("ё", "е")
    cleaned_name = " ".join(cleaned_name.split())
    return re.sub(rf"^{kind_word}\s+", "", cleaned_name)


def normalize_tariff(tariff_name: str, tariff_catalog: list[dict]) -> str:
    """Return the catalog name of an available tariff the model named."""
    available_tariffs = [
        tariff["name"] for tariff in tariff_catalog if tariff["available"]
    ]
    # I18N: the kind word in front of a tariff name.
    comparable_name = clean_name(tariff_name, "тариф")
    for tariff in tariff_catalog:
        if clean_name(tariff["name"], "тариф") != comparable_name:
            continue
        if not tariff["available"]:
            raise ActionError(
                ErrorCode.TARIFF_NOT_AVAILABLE,
                f"Тариф «{tariff['name']}» закрыт для подключения.",
                available=available_tariffs,
            )
        return tariff["name"]
    raise ActionError(
        ErrorCode.UNKNOWN_TARIFF,
        f"Тарифа «{tariff_name}» нет в каталоге.",
        available=available_tariffs,
    )


def normalize_service(service_name: str, service_catalog: list[str]) -> str:
    """Return the catalog name of a service the model named."""
    # I18N: the kind word in front of a service name.
    comparable_name = clean_name(service_name, "услуга")
    for service in service_catalog:
        if clean_name(service, "услуга") == comparable_name:
            return service
    raise ActionError(
        ErrorCode.UNKNOWN_SERVICE,
        f"Услуги «{service_name}» нет среди услуг номера.",
        available=service_catalog,
    )


def normalize_phone(phone: str) -> str:
    """Return a Russian mobile number in the stand's format."""
    digits = re.sub(r"\D", "", phone)
    if (
        not PHONE_CHARACTERS.fullmatch(phone.strip())
        or len(digits) != PHONE_DIGITS
        # 8 is the domestic prefix for +7.
        or digits[0] not in "78"
    ):
        raise ActionError(
            ErrorCode.BAD_PHONE,
            "Номер должен состоять из 11 цифр и начинаться с +7 или 8.",
            example=PHONE_EXAMPLE,
        )
    return f"+7 {digits[1:4]} {digits[4:7]}-{digits[7:9]}-{digits[9:]}"


def normalize_email(email: str) -> str:
    """Return the email trimmed and lowercased if it looks like an address."""
    cleaned_email = email.strip().lower()
    if not EMAIL_SHAPE.fullmatch(cleaned_email):
        raise ActionError(
            ErrorCode.BAD_EMAIL,
            "Адрес почты не похож на настоящий.",
            example=EMAIL_EXAMPLE,
        )
    return cleaned_email
