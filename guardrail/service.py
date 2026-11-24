"""Text check service: a rule engine, a lab heuristic, flags attacks in text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from core.logging import get_logger, shorten_for_log

logger = get_logger(__name__)

# POST-QA: GUARDRAIL_MODEL=1 switches to the HiveTraceGuard-Pro classifier,
# which judges meaning, not exact words; the rule engine stays the default.
ENGINE = "rule"


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


def read_check_request(body: object) -> tuple[str, CheckMode] | None:
    """Return the text and the mode of a check request, or None if invalid."""
    if not isinstance(body, dict) or not isinstance(body.get("text"), str):
        return None
    try:
        mode = CheckMode(body.get("mode"))
    except ValueError:
        return None
    return body["text"], mode


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
    text, mode = check_request
    unsafe_category = find_unsafe_category(text, mode)
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
            "engine": ENGINE,
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


def build_app() -> Starlette:
    """Return the service's web app."""
    return Starlette(
        routes=[
            Route("/check", check, methods=["POST"]),
            Route("/health", health, methods=["GET"]),
            Route("/admin/down", go_down, methods=["POST"]),
            Route("/admin/up", go_up, methods=["POST"]),
        ]
    )
