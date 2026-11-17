"""Answers to a live card are classified by code. No services are needed."""

import pytest

from app.answers import CardAnswer, classify_card_answer


@pytest.mark.parametrize(
    ("message", "answer"),
    [
        ("да", CardAnswer.YES),
        ("Да, пожалуйста!", CardAnswer.YES),
        ("не надо", CardAnswer.NO),
        # A cancel wins over a yes.
        ("да нет, не надо", CardAnswer.NO),
        ("позови оператора", CardAnswer.OPERATOR),
        # «да» counts only as a whole word.
        ("покажи мои данные", CardAnswer.TOPIC),
        # A yes word next to another request is no yes.
        ("давай лучше Игровой", CardAnswer.TOPIC),
        # A question is never an answer, whatever words it has.
        ("конечно, а сколько стоит Семейный?", CardAnswer.TOPIC),
        ("а оператор может вернуть деньги?", CardAnswer.TOPIC),
        # A long message only mentions an operator.
        ("у меня вчера оператор долго не отвечал", CardAnswer.TOPIC),
    ],
)
def test_classifies_answers(message, answer):
    """Only a plain answer counts; anything else is a topic for the agents."""
    assert classify_card_answer(message) is answer
