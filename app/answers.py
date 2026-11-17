"""Classifies a message sent while a confirmation card is live."""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import StrEnum

# I18N: the words and phrases of each answer, Russian.
YES_WORDS = (
    "да",
    "ага",
    "давай",
    "конечно",
    "подтверждаю",
    "подтверди",
    "верно",
    "угу",
    "продолжай",
)
# Phrases may have several words: «не надо».
NO_PHRASES = (
    "нет",
    "не надо",
    "отмена",
    "отменить",
    "откажись",
    "не нужно",
    "стоп",
)
OPERATOR_PHRASES = (
    "оператор",
    "человека",
    "живой человек",
    "поддержку",
    "менеджера",
    "специалиста",
)
# Polite words that may come with a yes: «да, пожалуйста».
YES_FILLER_WORDS = ("пожалуйста", "спасибо")
# A request for an operator is short: «позови оператора», «соедини с
# оператором». A longer message only mentions one.
OPERATOR_REQUEST_MAX_WORDS = 4


class CardAnswer(StrEnum):
    """What a message means for the live card."""

    YES = "yes"
    NO = "no"
    OPERATOR = "operator"
    TOPIC = "topic"  # anything else: a normal turn, and the card stays live


def build_phrase_pattern(
    phrases: Iterable[str], *, whole_word: bool
) -> re.Pattern:
    """Return a pattern that finds any phrase starting at a word boundary."""
    # Longest first, so "не надо" wins over a shorter phrase it contains.
    phrase_alternatives = "|".join(
        re.escape(phrase) for phrase in sorted(phrases, key=len, reverse=True)
    )
    word_end = r"(?!\w)" if whole_word else ""
    # (?<!\w) keeps "да" from matching inside "когда" or "тогда".
    return re.compile(
        rf"(?<!\w)(?:{phrase_alternatives}){word_end}", re.IGNORECASE
    )


# Whole words only: "нет" must not match "нетто".
NO_PATTERN = build_phrase_pattern(NO_PHRASES, whole_word=True)
# A word start is enough, so "оператора" and "оператору" match.
OPERATOR_PATTERN = build_phrase_pattern(OPERATOR_PHRASES, whole_word=False)
WORD_PATTERN = re.compile(r"\w+")


def is_pure_yes(words: list[str]) -> bool:
    """Return whether the words are a yes and nothing else."""
    # «давай лучше Игровой» has a yes word, but asks for something else.
    allowed_words = set(YES_WORDS) | set(YES_FILLER_WORDS)
    return bool(set(words) & set(YES_WORDS)) and set(words) <= allowed_words


def classify_card_answer(message: str) -> CardAnswer:
    """Return what the message says about the live card."""
    # A question is never an answer, even with «конечно» or «оператор» in it:
    # the agents take it and the card waits.
    if "?" in message:
        return CardAnswer.TOPIC
    # "No" goes first, so "да нет, не надо" cancels: a wrong cancel is cheaper
    # than a wrong execution.
    if NO_PATTERN.search(message):
        return CardAnswer.NO
    words = WORD_PATTERN.findall(message.lower())
    if is_pure_yes(words):
        return CardAnswer.YES
    if (
        OPERATOR_PATTERN.search(message)
        and len(words) <= OPERATOR_REQUEST_MAX_WORDS
    ):
        return CardAnswer.OPERATOR
    return CardAnswer.TOPIC
