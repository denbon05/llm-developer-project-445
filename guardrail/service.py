"""Text check service: flags attacks in text.

The rule engine, a lab heuristic, always runs first. With GUARDRAIL_MODEL=1
the HiveTraceGuard-Pro model also judges the text the rules let through.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, TypedDict

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from core import config
from core.logging import get_logger, shorten_for_log

logger = get_logger(__name__)

# The model's own boundary: unsafe once that answer is more likely than safe.
UNSAFE_THRESHOLD = 0.5


class CheckMode(StrEnum):
    """Whose text is checked."""

    INPUT = "input"  # the subscriber's message
    OUTPUT = "output"  # what a model wrote or a tool returned


class UnsafeCategory(StrEnum):
    """The kind of attack a rule finds."""

    JAILBREAK = "jailbreak"
    PROMPT_INJECTION = "prompt_injection"
    TOOL_HIJACK = "tool_hijack"
    INJECTED_INSTRUCTION = "injected_instruction"
    # The model found the text unsafe; it names no kind.
    MODEL_FLAGGED = "model_flagged"


class ChatMessage(TypedDict):
    """One message of the conversation the model judges."""

    role: str  # "user" or "assistant"
    content: str


class Classifier(Protocol):
    """A model that judges the last message of a conversation."""

    def find_unsafe_probability(self, messages: Sequence[ChatMessage]) -> float:
        """Return the probability, 0 to 1, that the last message is unsafe."""
        ...


@dataclass(frozen=True)
class CheckRequest:
    """What /check is asked to judge."""

    text: str
    mode: CheckMode
    # For an output text, the subscriber's message it replies to; None if not
    # given.
    subscriber_message: str | None


# A rule: the unsafe category it names and the text pattern that finds it.
AttackRule = tuple[UnsafeCategory, re.Pattern[str]]

# I18N: every rule below looks for a Russian word or a Russian word's stem;
# English text needs rules of its own.

# Anything but the end of a sentence: two words with this between them stand
# in one sentence.
SAME_SENTENCE = r"[^.!?]*"

# Tried in this order in both modes; the first match names the verdict.
ATTACK_RULES: tuple[AttackRule, ...] = (
    # Claims rights no subscriber has.
    (UnsafeCategory.JAILBREAK, re.compile("суперпользовател")),
    # Gives the model another role.
    (UnsafeCategory.JAILBREAK, re.compile("ты теперь админ")),
    # Asks for a mode without the rules.
    (UnsafeCategory.JAILBREAK, re.compile("режим разработчика")),
    # Tells the model to drop its instructions: «игнорируй» or «забудь», then
    # «инструкции» in the same sentence.
    (
        UnsafeCategory.PROMPT_INJECTION,
        re.compile(rf"(?:игнорируй|забудь){SAME_SENTENCE}инструкц"),
    ),
    # Asks for a change in an account, which only a confirmed card makes.
    (UnsafeCategory.TOOL_HIJACK, re.compile("смени тариф в аккаунте")),
    # Names the tool that only code calls, after a «да».
    (UnsafeCategory.TOOL_HIJACK, re.compile("execute_action")),
)

# A command verb as a whole word. The verb is required: a correct reply such
# as «Нет, сменить тариф без подтверждения нельзя…» must stay safe.
COMMAND_VERB = r"\b(?:переведи|смени|подключи|отключи|измени|выполни|исполни)\b"
# Words that tell the model to skip the card.
SKIP_CONFIRMATION = r"(?:без подтверждения|немедленно)"
# Tried after ATTACK_RULES, on output only. An injected instruction arrives
# through a tool, such as the archived «Про» article in the knowledge base, so
# it shows in what a model wrote. A subscriber who types «без подтверждения»
# skips nothing: the card still asks for «да».
OUTPUT_ONLY_ATTACK_RULES: tuple[AttackRule, ...] = (
    # A command to change the account without the card, in either order
    # within one sentence.
    (
        UnsafeCategory.INJECTED_INSTRUCTION,
        re.compile(rf"{COMMAND_VERB}{SAME_SENTENCE}{SKIP_CONFIRMATION}"),
    ),
    (
        UnsafeCategory.INJECTED_INSTRUCTION,
        re.compile(rf"{SKIP_CONFIRMATION}{SAME_SENTENCE}{COMMAND_VERB}"),
    ),
    # The header of a planted instruction, as in that article.
    (UnsafeCategory.INJECTED_INSTRUCTION, re.compile("системная инструкция")),
)


@dataclass
class Outage:
    """Whether the service imitates an outage."""

    is_down: bool = False


# The service's only state: while down, it answers 503.
OUTAGE = Outage()


def normalize(text: str) -> str:
    """Return the text in lowercase, «ё» as «е», each whitespace run a space."""
    # I18N: «ё» → «е» is for Russian text only.
    return re.sub(r"\s+", " ", text.lower().replace("ё", "е"))


def find_unsafe_category(text: str, mode: CheckMode) -> UnsafeCategory | None:
    """Return the unsafe category of the first rule that matches, or None."""
    normalized_text = normalize(text)
    tried_rules = (
        ATTACK_RULES + OUTPUT_ONLY_ATTACK_RULES
        if mode == CheckMode.OUTPUT
        else ATTACK_RULES
    )
    for unsafe_category, pattern in tried_rules:
        if pattern.search(normalized_text):
            return unsafe_category
    return None


def answer_down() -> JSONResponse:
    """Return the answer of a service that is down."""
    return JSONResponse({"status": "down"}, status_code=503)


def read_check_request(body: object) -> CheckRequest | None:
    """Return what a check request asks, or None if it is invalid."""
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        return None
    try:
        mode = CheckMode(body.get("mode"))
    except ValueError:
        return None
    subscriber_message = body.get("subscriber_message")
    if subscriber_message is not None and not isinstance(
        subscriber_message, str
    ):
        return None
    return CheckRequest(body["text"], mode, subscriber_message)


def build_messages(check_request: CheckRequest) -> list[ChatMessage]:
    """Return the conversation the model judges for this request."""
    text, subscriber_message = (
        check_request.text,
        check_request.subscriber_message,
    )
    if (
        check_request.mode == CheckMode.OUTPUT
        and subscriber_message is not None
    ):
        # The model's own way to judge a reply: after the message it answers.
        return [
            {"role": "user", "content": subscriber_message},
            {"role": "assistant", "content": text},
        ]
    # A subscriber's message, or an output text with nothing to answer: the
    # model judges it as a request.
    return [{"role": "user", "content": text}]


class ModelFailedError(Exception):
    """The model engine raised while judging a text."""


async def find_model_verdict(
    classifier: Classifier, check_request: CheckRequest
) -> UnsafeCategory | None:
    """Return MODEL_FLAGGED if the model finds the text unsafe, else None."""
    try:
        # A forward pass blocks for a few hundred milliseconds: off the event
        # loop, so /health and other checks still answer.
        unsafe_probability = await asyncio.to_thread(
            classifier.find_unsafe_probability, build_messages(check_request)
        )
    # Whatever broke inside the model, the check has no verdict.
    except Exception as error:  # pylint: disable=broad-exception-caught
        raise ModelFailedError(type(error).__name__) from error
    logger.info(
        "guardrail_model_scored",
        mode=check_request.mode,
        unsafe_probability=round(unsafe_probability, 3),
    )
    if unsafe_probability > UNSAFE_THRESHOLD:
        return UnsafeCategory.MODEL_FLAGGED
    return None


async def check(request: Request) -> JSONResponse:
    """Answer whether the text is safe and, if not, its unsafe category."""
    if OUTAGE.is_down:
        return answer_down()
    try:
        body = await request.json()
    except ValueError:
        body = None
    check_request = read_check_request(body)
    if check_request is None:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    text, mode = check_request.text, check_request.mode
    classifier: Classifier | None = request.app.state.classifier
    # Rules first: a match is a verdict with an exact category, and the model
    # isn't asked.
    unsafe_category = find_unsafe_category(text, mode)
    if unsafe_category is None and classifier is not None:
        try:
            unsafe_category = await find_model_verdict(
                classifier, check_request
            )
        except ModelFailedError as error:
            # No verdict: the client counts the service as unavailable, so
            # reading goes on and writes close.
            logger.error(
                "guardrail_model_failed", mode=mode, error_type=str(error)
            )
            return JSONResponse({"error": "model_failed"}, status_code=503)
    verdict = "safe" if unsafe_category is None else "unsafe"
    logger.info(
        "guardrail_verdict",
        mode=mode,
        verdict=verdict,
        category=unsafe_category,
    )
    # Development only: the text may hold personal data.
    logger.debug("guardrail_text", mode=mode, text=shorten_for_log(text))
    return JSONResponse(
        {
            "verdict": verdict,
            "category": unsafe_category,
            "engine": "rule" if classifier is None else "rule+model",
            "mode": mode,
        }
    )


async def health(_request: Request) -> JSONResponse:
    """Answer whether the service works."""
    if OUTAGE.is_down:
        return answer_down()
    return JSONResponse({"status": "ok"})


async def go_down(_request: Request) -> JSONResponse:
    """Start imitating an outage."""
    OUTAGE.is_down = True
    return JSONResponse({"ok": True, "down": OUTAGE.is_down})


async def go_up(_request: Request) -> JSONResponse:
    """Stop imitating an outage."""
    OUTAGE.is_down = False
    return JSONResponse({"ok": True, "down": OUTAGE.is_down})


def load_classifier() -> Classifier:
    """Load the model engine; raise if it can't be loaded."""
    # Imported here, not at the top: torch and transformers aren't in
    # requirements.txt, so Hexlet's checker, which runs the rule engine only,
    # doesn't have them.
    # POST-QA: import at the top once they are regular dependencies.
    from guardrail.classifier import (  # pylint: disable=import-outside-toplevel
        HiveTraceClassifier,
    )

    return HiveTraceClassifier.load()


def make_app(classifier: Classifier | None) -> Starlette:
    """Return the service's web app; without a classifier, rules only."""
    app = Starlette(
        routes=[
            Route("/check", check, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
            Route("/admin/down", go_down, methods=["POST"]),
            Route("/admin/up", go_up, methods=["POST"]),
        ]
    )
    app.state.classifier = classifier
    return app


def build_app() -> Starlette:
    """Return the service's web app, with the model if GUARDRAIL_MODEL is set."""
    # A model that fails to load stops the service: falling back to rules
    # alone would hide that the check is weaker than asked.
    return make_app(load_classifier() if config.GUARDRAIL_MODEL else None)
