"""Client of the text check service: a verdict on a text, never an exception."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import httpx2

from core import config
from core.logging import get_logger, shorten_for_log

logger = get_logger(__name__)

# The rules answer at once, the model in a few hundred milliseconds; a stuck
# service must not hang the turn.
GUARDRAIL_TIMEOUT_SEC = 2


class CheckMode(StrEnum):
    """Whose text is checked."""

    INPUT = "input"  # the subscriber's message
    OUTPUT = "output"  # what a model wrote or a tool returned


@dataclass(frozen=True)
class Verdict:
    """The check's verdict on a text."""

    safe: bool
    category: str | None  # why the text is unsafe, None for safe text
    available: bool  # whether the service answered


# Without an answer the text counts as safe, so reading goes on; `available`
# False is what closes writes.
UNAVAILABLE = Verdict(safe=True, category=None, available=False)


def build_guardrail_client() -> httpx2.AsyncClient:
    """Return an HTTP client for the text check service."""
    # trust_env=False: a system proxy must not intercept calls to 127.0.0.1.
    return httpx2.AsyncClient(
        base_url=config.GUARDRAIL_URL,
        trust_env=False,
        timeout=GUARDRAIL_TIMEOUT_SEC,
    )


def read_verdict(response: httpx2.Response) -> Verdict | None:
    """Return the verdict in the service's answer, or None if it holds none."""
    if response.status_code != httpx2.codes.OK:
        return None
    try:
        answer = response.json()
    except ValueError:
        return None
    verdict = answer.get("verdict") if isinstance(answer, dict) else None
    if verdict not in ("safe", "unsafe"):
        return None
    unsafe_category = answer.get("category")
    return Verdict(
        safe=verdict == "safe",
        category=unsafe_category if isinstance(unsafe_category, str) else None,
        available=True,
    )


def report_unavailable(mode: CheckMode, **failure: object) -> Verdict:
    """Log why the service gave no verdict and return the verdict for that."""
    logger.warning("guardrail_unavailable", mode=mode, **failure)
    return UNAVAILABLE


async def check(
    text: str, mode: CheckMode, *, subscriber_message: str | None = None
) -> Verdict:
    """Return the service's verdict on the text in this mode.

    subscriber_message: for an output text, the subscriber's message it replies to,
    such as «какой у меня тариф?» for «Ваш тариф «Базовый».». The service's
    model then judges the text as that reply.
    """
    check_body: dict[str, str] = {"text": text, "mode": mode}
    if subscriber_message is not None:
        check_body["subscriber_message"] = subscriber_message
    try:
        async with build_guardrail_client() as guardrail_client:
            response = await guardrail_client.post("/check", json=check_body)
    except httpx2.HTTPError as error:
        # No connection or a timeout.
        return report_unavailable(mode, error_type=type(error).__name__)
    verdict = read_verdict(response)
    if verdict is None:
        # Another status, or an answer without a verdict.
        return report_unavailable(mode, status=response.status_code)
    # Development only: the text may hold personal data.
    logger.debug(
        "guardrail_checked",
        mode=mode,
        verdict="safe" if verdict.safe else "unsafe",
        category=verdict.category,
        text=shorten_for_log(text),
    )
    return verdict
