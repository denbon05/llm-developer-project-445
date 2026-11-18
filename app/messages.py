"""Fixed text the subscriber sees. Russian until the project moves to English."""

from actions.contracts import ExecutionResult, Outcome

TURN_FAILED = "Не удалось обработать запрос. Попробуйте ещё раз."
UNKNOWN_SESSION = "Сессия не найдена. Обновите страницу."
UNKNOWN_USER = "Неизвестный пользователь."

CARD_READY = (
    "Проверьте карточку выше. Ответьте «да», чтобы подтвердить, "
    "или «нет», чтобы отменить."
)
CARD_REJECTED = "Отменил, ничего не изменилось."
CARD_NOT_FOUND = "Карточка не найдена или истекла."
CARD_EXPIRED = "Время на подтверждение вышло. Повторите, пожалуйста, просьбу."
TICKET_CREATED = (
    "Передал обращение оператору, номер {ticket_id}. С вами свяжутся."
)
TICKET_FAILED = "Не удалось передать обращение оператору. Попробуйте ещё раз."

TARIFF_CHANGED = (
    "Готово: тариф изменён с «{before}» на «{after}», {price_after} ₽/мес."
)
SERVICE_ENABLED = "Готово: услуга «{service}» подключена."
SERVICE_DISABLED = "Готово: услуга «{service}» отключена."
CONTACTS_UPDATED = (
    "Готово: контакты обновлены — телефон {phone}, почта {email}."
)
ACTION_DUPLICATE = "Эта операция уже исполнена — повтор не выполнялся."
ACTION_DAILY_LIMIT = (
    "Лимит смен тарифа на сегодня исчерпан. "
    "Попробуйте завтра или напишите «оператор»."
)
ACTION_FAILED = "Не удалось выполнить операцию. Ответьте «да», чтобы повторить."
# Refusals at execution time: the state changed after the card was prepared.
EXECUTION_REFUSALS: dict[str, str] = {
    Outcome.SAME_TARIFF: "Этот тариф уже подключён, менять нечего.",
    Outcome.TARIFF_NOT_AVAILABLE: "Этот тариф закрыт для подключения.",
    Outcome.UNKNOWN_TARIFF: "Такого тарифа нет в каталоге.",
    Outcome.UNKNOWN_SERVICE: "Эта услуга недоступна на вашем номере.",
    Outcome.NOT_FOUND: "Абонент не найден, изменение не выполнено.",
}


def build_execution_reply(result: ExecutionResult) -> str:
    """Return the reply for the outcome of an executed action."""
    outcome = result["outcome"]
    if outcome == Outcome.EXECUTED:
        return TARIFF_CHANGED.format(**result)
    if outcome == Outcome.TOGGLED:
        template = (
            SERVICE_ENABLED if result.get("enabled") else SERVICE_DISABLED
        )
        return template.format(**result)
    if outcome == Outcome.UPDATED:
        return CONTACTS_UPDATED.format(**result)
    if outcome == Outcome.DUPLICATE:
        return ACTION_DUPLICATE
    if outcome == Outcome.DAILY_LIMIT:
        return ACTION_DAILY_LIMIT
    return EXECUTION_REFUSALS.get(outcome, ACTION_FAILED)
