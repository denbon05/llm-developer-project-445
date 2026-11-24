"""What the PEP sends to the PDP and how it reads the answers.

A stub PDP on httpx2.MockTransport answers, so no services are needed.
"""

import asyncio
import json

import httpx2
import pytest
from structlog.contextvars import bound_contextvars

from app import pep
from app.cards import Card
from app.pep import Pep
from app.permissions import Decision
from core import config

SESSION_ID = "sess_1a2b3c4d"
ANNA_CLAIMS = {
    "sub": "u_4f2a",
    "role": "client",
    "scope": "tariffs:read tariffs:write charges:read",
}
# Details whose keys are not in sorted order.
CARD = Card(
    pending_id="p_1a2b3c4d",
    action="contacts_update",
    details={"phone": "+7 900 111-22-33", "email": "anna.new@example.ru"},
    view={"Телефон": "+7 900 111-22-33", "Почта": "anna.new@example.ru"},
    session_id=SESSION_ID,
    sub="u_4f2a",
)

# The PDP's answers: a status and a JSON body.
SESSION_OPENED = (200, {"ok": True, "session_id": SESSION_ID})
ALLOW = (200, {"decision": "Allow", "reason": ""})
SESSION_LIMIT = (200, {"decision": "Deny", "reason": "session_limit"})
SERVER_ERROR = (500, {"error": "internal"})
SESSION_FORGOTTEN = (404, {"error": f"нет сессии {SESSION_ID}"})

ALLOWED = Decision(allowed=True)
UNAVAILABLE = Decision(allowed=False, reason="pdp_unavailable")


class StubTokens:
    """Gives anna's claims, as the issuer puts them in her token."""

    async def claims(self) -> dict:
        return ANNA_CLAIMS


class StubPdp:
    """Answers the PEP's requests in order and remembers each one."""

    def __init__(
        self, answers: list[tuple[int, dict] | type[httpx2.TransportError]]
    ) -> None:
        # A status and a body, or a transport failure the client raises.
        self.answers = list(answers)
        # Every request in order: its path and its JSON body.
        self.requests: list[tuple[str, dict]] = []

    def answer(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append((request.url.path, json.loads(request.content)))
        pdp_answer = self.answers.pop(0)
        if isinstance(pdp_answer, tuple):
            status_code, body = pdp_answer
            return httpx2.Response(status_code, json=body)
        raise pdp_answer("the PDP failed", request=request)


def install_stub_pdp(monkeypatch, stub_pdp: StubPdp) -> None:
    """Make the PEP's HTTP clients reach the stub PDP."""
    monkeypatch.setattr(
        pep,
        "build_pdp_client",
        lambda: httpx2.AsyncClient(
            base_url=config.PDP_URL,
            transport=httpx2.MockTransport(stub_pdp.answer),
        ),
    )


def make_pep() -> Pep:
    """Return anna's PEP."""
    return Pep(SESSION_ID, StubTokens())  # pyright: ignore[reportArgumentType]


def test_pep_sends_the_card_and_the_subject(monkeypatch):
    """The PDP gets the subject once, then the card and the turn's flag."""
    stub_pdp = StubPdp([SESSION_OPENED, ALLOW, ALLOW, ALLOW])
    install_stub_pdp(monkeypatch, stub_pdp)
    card_pep = make_pep()

    async def pass_the_card() -> None:
        await card_pep.prepare(CARD)
        # A "да" in a turn whose text check got no answer.
        await card_pep.confirm(CARD, guard_ok=False)
        await card_pep.execute(CARD)

    # A turn binds its trace id to the log context.
    with bound_contextvars(trace_id="tr_1a2b3c4d5e6f"):
        asyncio.run(pass_the_card())

    subject = {
        "sub": "u_4f2a",
        "role": "client",
        "scopes": ["tariffs:read", "tariffs:write", "charges:read"],
    }
    policy_input = {
        "action": "contacts_update",
        "pending_id": "p_1a2b3c4d",
        # Sorted keys: a card read back from the database gives the same.
        "details": (
            '{"email": "anna.new@example.ru", "phone": "+7 900 111-22-33"}'
        ),
        "guard_ok": True,
        **subject,
    }
    stage_request = {
        "session_id": SESSION_ID,
        "input": policy_input,
        "trace_id": "tr_1a2b3c4d5e6f",
    }
    assert stub_pdp.requests == [
        ("/session", {"session_id": SESSION_ID, "subject": subject}),
        ("/authorize", {**stage_request, "action": "Prepare"}),
        # A "да" has its own endpoint, which needs no action.
        (
            "/confirm",
            {**stage_request, "input": {**policy_input, "guard_ok": False}},
        ),
        ("/authorize", {**stage_request, "action": "Execute"}),
    ]


@pytest.mark.parametrize(
    ("pdp_answers", "expected_decisions", "expected_paths"),
    [
        # The PDP's decision, with its reason.
        (
            [SESSION_OPENED, SESSION_LIMIT],
            [Decision(allowed=False, reason="session_limit")],
            ["/session", "/authorize"],
        ),
        # No connection.
        ([httpx2.ConnectError], [UNAVAILABLE], ["/session"]),
        # No answer in time.
        (
            [SESSION_OPENED, httpx2.ReadTimeout],
            [UNAVAILABLE],
            ["/session", "/authorize"],
        ),
        # A server error.
        (
            [SESSION_OPENED, SERVER_ERROR],
            [UNAVAILABLE],
            ["/session", "/authorize"],
        ),
        # The PDP restarted and forgot the session: it is opened again and
        # the request repeated once.
        (
            [SESSION_OPENED, SESSION_FORGOTTEN, SESSION_OPENED, ALLOW],
            [ALLOWED],
            ["/session", "/authorize", "/session", "/authorize"],
        ),
        # A failed open is tried again on the next request.
        (
            [SERVER_ERROR, SESSION_OPENED, ALLOW],
            [UNAVAILABLE, ALLOWED],
            ["/session", "/session", "/authorize"],
        ),
    ],
)
def test_pep_closes_writes_without_a_decision(
    monkeypatch, pdp_answers, expected_decisions, expected_paths
):
    """Anything but a decision from the PDP is a refusal."""
    stub_pdp = StubPdp(pdp_answers)
    install_stub_pdp(monkeypatch, stub_pdp)
    card_pep = make_pep()

    decisions = [
        asyncio.run(card_pep.prepare(CARD)) for _ in expected_decisions
    ]

    assert decisions == expected_decisions
    assert [path for path, _ in stub_pdp.requests] == expected_paths
