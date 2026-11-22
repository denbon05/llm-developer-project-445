"""Policy enforcement: asks the policy service before each stage of a card."""

from __future__ import annotations

import json
from enum import StrEnum
from typing import TYPE_CHECKING

import httpx2

from app.permissions import Decision
from core import config
from core.logging import get_logger, read_trace_id

if TYPE_CHECKING:
    from app.cards import Card
    from core.tokens import Tokens

logger = get_logger(__name__)

# The PDP answers at once; a change must not hang the turn on a stuck one.
PDP_TIMEOUT_SEC = 3


class PolicyReason(StrEnum):
    """The refusal reasons code branches on."""

    SESSION_LIMIT = "session_limit"
    CONFIRMATION_EXPIRED = "confirmation_expired"
    CARD_NOT_PREPARED = "card_not_prepared"
    PDP_UNAVAILABLE = "pdp_unavailable"


def build_pdp_client() -> httpx2.AsyncClient:
    """Return an HTTP client for the PDP."""
    # trust_env=False: a system proxy must not intercept calls to 127.0.0.1.
    return httpx2.AsyncClient(
        base_url=config.PDP_URL, trust_env=False, timeout=PDP_TIMEOUT_SEC
    )


def read_decision(response: httpx2.Response) -> Decision | None:
    """Return the decision in the PDP's answer, or None if it holds none."""
    if response.status_code != httpx2.codes.OK:
        return None
    try:
        answer = response.json()
    except ValueError:
        return None
    verdict = answer.get("decision") if isinstance(answer, dict) else None
    if verdict not in ("Allow", "Deny"):
        return None
    return Decision(
        allowed=verdict == "Allow", reason=str(answer.get("reason", ""))
    )


def refuse_as_unavailable(action: str, **failure: object) -> Decision:
    """Log why the PDP gave no decision and return a refusal."""
    logger.warning("pdp_unavailable", action=action, **failure)
    # Fail closed: without the PDP's answer nothing is written.
    return Decision(allowed=False, reason=PolicyReason.PDP_UNAVAILABLE)


class Pep:
    """Asks the PDP before each stage of a card and returns its decision.

    The PEP decides nothing itself: the caller does what the answer says.
    """

    def __init__(self, session_id: str, tokens: Tokens) -> None:
        self.session_id = session_id
        self.tokens = tokens
        self.is_pdp_session_open = False

    async def prepare(self, card: Card) -> Decision:
        """Ask whether the card may exist."""
        # The policy allows a client with the write scope. The PDP records an
        # allowed card in its journal: a later "да" must match it.
        return await self.decide("Prepare", card)

    async def confirm(self, card: Card) -> Decision:
        """Ask whether a "да" may confirm the card."""
        # The PDP first checks its journal: the card was allowed and is still
        # in time. Then the policy: the same card with the same details.
        return await self.decide("ConfirmCard", card)

    async def execute(self, card: Card) -> Decision:
        """Ask whether the confirmed card may run."""
        # The policy allows a confirmed card unless it is the session's third
        # distinct tariff change.
        return await self.decide("Execute", card)

    async def decide(self, action: str, card: Card) -> Decision:
        """Ask the PDP about one stage of the card and log its decision."""
        decision = await self.request_decision(action, card)
        logger.info(
            "policy_decision",
            action=action,
            pending_id=card.pending_id,
            decision="Allow" if decision.allowed else "Deny",
            reason=str(decision.reason),
        )
        return decision

    async def request_decision(self, action: str, card: Card) -> Decision:
        """Send the PDP the request for one stage and return its decision."""
        request_body = {
            "session_id": self.session_id,
            "input": await self.build_policy_input(card),
            # The turn that asks, for the PDP's journal of decisions.
            "trace_id": read_trace_id(),
        }
        # A "да" has an endpoint of its own: there the PDP checks its journal
        # of cards before the policy.
        if action == "ConfirmCard":
            path = "/confirm"
        else:
            path = "/authorize"
            request_body["action"] = action
        try:
            response = await self.post_in_pdp_session(path, request_body)
        except httpx2.HTTPError as error:
            # No connection, a timeout, or the session could not be opened.
            return refuse_as_unavailable(
                action, error_type=type(error).__name__
            )
        decision = read_decision(response)
        if decision is None:
            # Another status, or an answer without a decision.
            return refuse_as_unavailable(action, status=response.status_code)
        return decision

    async def post_in_pdp_session(
        self, path: str, request_body: dict
    ) -> httpx2.Response:
        """Post a request to the PDP, opening the PDP session when needed."""
        async with build_pdp_client() as pdp_client:
            # The PDP session is opened on the first decision, not with the
            # chat: reads never ask the PDP, so a chat that only reads works
            # while the PDP is down.
            if not self.is_pdp_session_open:
                await self.open_pdp_session(pdp_client)
            response = await pdp_client.post(path, json=request_body)
            if response.status_code == httpx2.codes.NOT_FOUND:
                # The PDP restarted and forgot the session: open it again and
                # repeat the request once. The PDP forgot the session's cards
                # too, so a "да" to one of them gets card_not_prepared.
                self.is_pdp_session_open = False
                await self.open_pdp_session(pdp_client)
                response = await pdp_client.post(path, json=request_body)
        return response

    async def open_pdp_session(self, pdp_client: httpx2.AsyncClient) -> None:
        """Open the session in the PDP with the token's subject."""
        response = await pdp_client.post(
            "/session",
            json={
                "session_id": self.session_id,
                "subject": await self.read_subject(),
            },
        )
        response.raise_for_status()
        # Marked only after success, so a failed open is tried again on the
        # next request.
        self.is_pdp_session_open = True

    async def read_subject(self) -> dict:
        """Return the subject facts of the session's token."""
        claims = await self.tokens.claims()
        return {
            "sub": str(claims.get("sub", "")),
            "role": str(claims.get("role", "")),
            # `scope` is one space-separated string, as OAuth sends it.
            "scopes": str(claims.get("scope", "")).split(),
        }

    async def build_policy_input(self, card: Card) -> dict:
        """Return what the policy reads about the card and its subject."""
        subject = await self.read_subject()
        return {
            "action": card.action,
            "pending_id": card.pending_id,
            # The policy compares this string between the stages, and jsonb
            # returns keys in its own order: sorted keys give a card read
            # back from the database the same string as the new one.
            "details": json.dumps(
                card.details, ensure_ascii=False, sort_keys=True
            ),
            # No text check exists yet, and the policy doesn't read it.
            "guard_ok": True,
            **subject,
        }
