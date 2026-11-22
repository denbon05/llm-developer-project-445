"""A fake PEP that gives set decisions instead of asking the PDP."""

from app.cards import Card
from app.pep import Pep
from app.permissions import Decision


class FakePep(Pep):
    """Gives each stage its set decision; a stage without one is allowed."""

    def __init__(
        self, decision_by_action: dict[str, Decision] | None = None
    ) -> None:
        # No PDP session and no tokens: nothing is sent.
        super().__init__(session_id="", tokens=None)  # pyright: ignore[reportArgumentType]
        # The stage, such as "ConfirmCard", and the decision it gets.
        self.decision_by_action = decision_by_action or {}

    async def request_decision(self, action: str, card: Card) -> Decision:
        return self.decision_by_action.get(action, Decision(allowed=True))
