"""Confirmation cards in the pending_cards table: stored, found and settled."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from core import config, db

# The columns build_card reads, in every query that returns cards. The
# database counts the seconds left, with the same clock that set expires_at.
CARD_COLUMNS: Final = (
    "pending_id, session_id, sub, action, details, card, state,"
    " greatest(ceil(extract(epoch from expires_at - now())), 0)::int"
    " as seconds_left"
)


class CardState(StrEnum):
    """A card's state in the pending_cards table."""

    PENDING = "pending"  # waits for an answer
    CONFIRMED = "confirmed"  # "да", and the account has the change
    REJECTED = "rejected"  # "нет", or handed to an operator
    REPLACED = "replaced"  # a newer card of the session took its place


@dataclass
class Card:
    """A confirmation card as the pending_cards table holds it."""

    pending_id: str
    action: str
    details: dict  # what execution gets
    view: dict  # what the subscriber reads: row label → value
    session_id: str
    sub: str
    state: CardState = CardState.PENDING
    # Seconds the card had left when it was read; 0 once it expired.
    seconds_left: int = config.CARD_TTL_SEC

    @property
    def is_expired(self) -> bool:
        """Return whether the card had expired when it was read."""
        return self.seconds_left == 0

    @property
    def is_live(self) -> bool:
        """Return whether the card waits for an answer: pending, not expired."""
        return self.state == CardState.PENDING and not self.is_expired

    def public_view(self) -> dict:
        """Return the card as the web chat shows it."""
        return {
            **self.view,
            "action": self.action,
            "ttl_sec": self.seconds_left,
        }


def to_json(value: dict) -> str:
    """Return the value as JSON text that keeps Cyrillic readable."""
    return json.dumps(value, ensure_ascii=False)


# POST-QA: rename to create_card. tests/smoke.py calls this name.
def create(session, action: str, details: dict, view: dict) -> Card:
    """Store a new pending card for the session and return it."""
    pending_id = f"p_{uuid.uuid4().hex[:8]}"
    with db.connect() as conn, conn.cursor() as cur:
        # A session waits for one answer at a time: the new card replaces
        # the one still pending.
        cur.execute(
            "update pending_cards set state = %s"
            " where session_id = %s and state = %s",
            (CardState.REPLACED, session.session_id, CardState.PENDING),
        )
        cur.execute(
            "insert into pending_cards"
            "(pending_id, session_id, sub, action, details, card, expires_at)"
            " values (%s, %s, %s, %s, %s, %s, now() + make_interval(secs => %s))",
            (
                pending_id,
                session.session_id,
                session.user.sub,
                action,
                to_json(details),
                to_json(view),
                config.CARD_TTL_SEC,
            ),
        )
        conn.commit()
    return Card(
        pending_id, action, details, view, session.session_id, session.user.sub
    )


def build_card(card_row: dict) -> Card:
    """Return the card a row of CARD_COLUMNS describes."""
    return Card(
        pending_id=card_row["pending_id"],
        action=card_row["action"],
        details=card_row["details"],
        view=card_row["card"],
        session_id=card_row["session_id"],
        sub=card_row["sub"],
        # A state this module doesn't know fails here, not somewhere later.
        state=CardState(card_row["state"]),
        seconds_left=card_row["seconds_left"],
    )


def find_latest_card(session_id: str) -> Card | None:
    """Return the session's newest card in any state, expired or not."""
    card_row = db.fetchone(
        f"select {CARD_COLUMNS} from pending_cards"
        " where session_id = %s order by created_at desc limit 1",
        (session_id,),
    )
    return build_card(card_row) if card_row else None


def find_card(session_id: str, pending_id: str) -> Card | None:
    """Return the session's card with this id in any state, expired or not."""
    card_row = db.fetchone(
        f"select {CARD_COLUMNS} from pending_cards"
        " where pending_id = %s and session_id = %s",
        (pending_id, session_id),
    )
    return build_card(card_row) if card_row else None


def settle_card(card: Card, state: CardState) -> None:
    """Record how the card ended."""
    db.execute(
        "update pending_cards set state = %s where pending_id = %s",
        (state, card.pending_id),
    )
