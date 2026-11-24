"""How the guardrail client reads the text check service's answers.

A fake service on httpx2.MockTransport answers, so no services are needed.
"""

import asyncio

import httpx2
import pytest

from app import guardrail_client
from app.guardrail_client import CheckMode, Verdict
from core import config

UNAVAILABLE = Verdict(safe=True, category=None, available=False)


def install_fake_service(
    monkeypatch, service_answer: httpx2.Response | type[httpx2.TransportError]
) -> None:
    """Make the client reach a fake service that gives this answer."""

    def answer(request: httpx2.Request) -> httpx2.Response:
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
    install_fake_service(monkeypatch, service_answer)

    verdict = asyncio.run(
        guardrail_client.check("какой у меня тариф?", mode=CheckMode.INPUT)
    )

    assert verdict == expected_verdict
