"""How the guardrail client reads the text check service's answers.

A stub service on httpx2.MockTransport answers, so no services are needed.
"""

import asyncio
import json

import httpx2
import pytest

from app import guardrail_client
from app.guardrail_client import CheckMode, Verdict
from core import config

UNAVAILABLE = Verdict(safe=True, category=None, available=False)


def install_stub_service(
    monkeypatch, service_answer: httpx2.Response | type[httpx2.TransportError]
) -> list[dict]:
    """Make the client reach a stub service that gives this answer.

    Return the list the stub fills with the request bodies it receives.
    """
    received_bodies = []

    def answer(request: httpx2.Request) -> httpx2.Response:
        received_bodies.append(json.loads(request.content))
        if isinstance(service_answer, httpx2.Response):
            return service_answer
        raise service_answer("the service failed", request=request)

    monkeypatch.setattr(
        guardrail_client,
        "build_guardrail_client",
        lambda: httpx2.AsyncClient(
            base_url=config.GUARDRAIL_URL,
            transport=httpx2.MockTransport(answer),
        ),
    )
    return received_bodies


@pytest.mark.parametrize(
    ("service_answer", "expected_verdict"),
    [
        # Safe text.
        (
            httpx2.Response(
                200,
                json={
                    "verdict": "safe",
                    "category": None,
                    "engine": "rule",
                    "mode": "input",
                },
            ),
            Verdict(safe=True, category=None, available=True),
        ),
        # Unsafe text, with the attack's category.
        (
            httpx2.Response(
                200,
                json={
                    "verdict": "unsafe",
                    "category": "jailbreak",
                    "engine": "rule",
                    "mode": "input",
                },
            ),
            Verdict(safe=False, category="jailbreak", available=True),
        ),
        # No connection.
        (httpx2.ConnectError, UNAVAILABLE),
        # No answer in time.
        (httpx2.ReadTimeout, UNAVAILABLE),
        # The service is down.
        (httpx2.Response(503, json={"status": "down"}), UNAVAILABLE),
        # An answer that isn't JSON.
        (httpx2.Response(200, text="<html>bad gateway</html>"), UNAVAILABLE),
    ],
)
def test_client_reads_the_verdict(
    monkeypatch, service_answer, expected_verdict
):
    """Anything but a verdict is a check that didn't answer, never an error."""
    install_stub_service(monkeypatch, service_answer)

    verdict = asyncio.run(
        guardrail_client.check("какой у меня тариф?", mode=CheckMode.INPUT)
    )

    assert verdict == expected_verdict


def test_client_sends_the_subscriber_message(monkeypatch):
    """The message an output text replies to reaches the service with it."""
    received_bodies = install_stub_service(
        monkeypatch,
        httpx2.Response(
            200,
            json={
                "verdict": "safe",
                "category": None,
                "engine": "rule",
                "mode": "output",
            },
        ),
    )

    subscriber_message = "какой у меня тариф?"
    reply = "Ваш тариф «Базовый»."

    asyncio.run(
        guardrail_client.check(
            reply, mode=CheckMode.OUTPUT, subscriber_message=subscriber_message
        )
    )

    # The body holds the reply as the text and the message it replies to.
    assert received_bodies == [
        {
            "text": reply,
            "mode": "output",
            "subscriber_message": subscriber_message,
        }
    ]
