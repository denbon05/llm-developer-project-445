"""The write-path policy on the real Dogwood authorizer. No services are needed.

The PDP's own checks and its reasons are covered by smoke group 4.
"""

import pytest
from dogwood import native

from pdp.service import POLICY_FILE, SCHEMA_FILE

PRINCIPAL = 'Tel::OAuthUser::"u_4f2a"'
RESOURCE = 'Tel::Gateway::"pdp"'
TARIFF_DETAILS = '{"tariff": "Семейный"}'


def build_input(
    pending_id: str,
    action: str = "tariff_change",
    details: str = TARIFF_DETAILS,
    *,
    guard_ok: bool = True,
) -> dict:
    """Return the policy input for anna's card."""
    return {
        "action": action,
        "pending_id": pending_id,
        "details": details,
        "guard_ok": guard_ok,
        "sub": "u_4f2a",
        "role": "client",
        "scopes": ["tariffs:read", "tariffs:write", "charges:read"],
    }


def build_card_requests(
    pending_id: str, action: str = "tariff_change", yes_count: int = 1
) -> list[tuple[str, dict]]:
    """Return a card's Prepare, then ConfirmCard and Execute for each "да"."""
    policy_input = build_input(pending_id, action)
    return [
        ("Prepare", policy_input),
        *[("ConfirmCard", policy_input), ("Execute", policy_input)] * yes_count,
    ]


TWO_TARIFF_CHANGES = [*build_card_requests("p_1"), *build_card_requests("p_2")]


@pytest.mark.parametrize(
    ("requests", "expected_last_decision"),
    [
        # Three "да" to one card take one slot of the limit, so a second card
        # still runs.
        (
            [
                *build_card_requests("p_1", yes_count=3),
                *build_card_requests("p_2"),
            ],
            "Allow",
        ),
        # The third distinct tariff card is over the limit.
        ([*TWO_TARIFF_CHANGES, *build_card_requests("p_3")], "Deny"),
        # A service toggle after the limit: the limit counts tariffs only.
        (
            [
                *TWO_TARIFF_CHANGES,
                *build_card_requests("p_3"),
                *build_card_requests("p_4", action="service_toggle"),
            ],
            "Allow",
        ),
        # A "да" binds to the card as it was prepared, details included.
        (
            [
                ("Prepare", build_input("p_1")),
                (
                    "ConfirmCard",
                    build_input("p_1", details='{"tariff": "Игровой"}'),
                ),
            ],
            "Deny",
        ),
        # Every stage needs a turn whose text check answered: Prepare...
        ([("Prepare", build_input("p_1", guard_ok=False))], "Deny"),
        # ...ConfirmCard...
        (
            [
                ("Prepare", build_input("p_1")),
                ("ConfirmCard", build_input("p_1", guard_ok=False)),
            ],
            "Deny",
        ),
        # ...and Execute.
        (
            [
                ("Prepare", build_input("p_1")),
                ("ConfirmCard", build_input("p_1")),
                ("Execute", build_input("p_1", guard_ok=False)),
            ],
            "Deny",
        ),
    ],
)
def test_policy_decides_the_last_request(requests, expected_last_decision):
    """Each session's authorizer remembers its requests, allowed or denied."""
    authorizer = native.NativeAuthorizer(
        POLICY_FILE.read_text(encoding="utf-8"),
        SCHEMA_FILE.read_text(encoding="utf-8"),
    )

    decisions = [
        authorizer.authorize_request(
            f"Tel::Action::{action}", PRINCIPAL, RESOURCE, policy_input
        )
        for action, policy_input in requests
    ]

    assert decisions[-1] == expected_last_decision
