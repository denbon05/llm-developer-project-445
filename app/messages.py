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
CARD_NOT_PREPARED = (
    "Карточка не найдена или истекла. Повторите, пожалуйста, просьбу."
)
TICKET_CREATED = (
    "Передал обращение оператору, номер {ticket_id}. С вами свяжутся."
)
TICKET_FAILED = "Не удалось передать обращение оператору. Попробуйте ещё раз."
# The report of a run stopped by a denied tool call. It names no id and no
# reason, so it doesn't confirm that another subscriber exists.
OPERATION_DENIED = (
    "Операция не выполнена: в чате можно работать только со своим аккаунтом "
    "и только с разрешёнными вам операциями."
)
# Policy refusals name no internal service: the reason stays in the journals.
# The number is the policy's limit of tariff changes per session.
SESSION_TARIFF_LIMIT = (
    "В этом чате можно сменить тариф не больше двух раз. "
    "Могу передать запрос оператору — напишите «оператор»."
)
CHANGE_UNAVAILABLE = (
    "Сейчас не получается выполнить изменение. "
    "Попробуйте позже или напишите «оператор»."
)
CHANGE_UNAVAILABLE_RETRY = (
    "Сейчас не получается выполнить изменение. "
    "Ответьте «да» позже, чтобы повторить, или напишите «оператор»."
)
# Refusals after a text check don't say that the check exists or is down: the
# subscriber can't act on it, and it would tell an attacker the filter's state.
INPUT_REFUSED = (
    "Не могу выполнить такую просьбу. Я помогаю с тарифом, "
    "списаниями, услугами и контактами."
)
OUTPUT_REFUSED = (
    "Не получилось подготовить ответ. Переформулируйте, пожалуйста, "
    "вопрос или напишите «оператор»."
)

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
