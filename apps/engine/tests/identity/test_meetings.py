"""The meeting hook refuses to invent an ordinal (PHASE-0-SPEC.md §17.5).

No database and no provider: the rule is about competition format, so it is
testable as pure data. The Scottish case is the real one - verified against
SC0 2023/24, where the same ordered pairing occurs two and three times.
"""

from __future__ import annotations

from engine.ingestion.meetings import (
    MeetingIdentity,
    MeetingPlan,
    RepeatedPairing,
)

E0_LIKE = [("Burnley", "Man City"), ("Arsenal", "Forest"), ("Man City", "Burnley")]
SC0_LIKE = [
    ("Celtic", "Rangers"),
    ("Celtic", "Rangers"),
    ("Celtic", "Rangers"),
    ("Hearts", "Hibernian"),
]


class TestUniquePairings:
    def test_a_pairing_seen_once_is_identified(self) -> None:
        plan = MeetingPlan.from_pairings(E0_LIKE)
        assert plan.resolve(("Burnley", "Man City")) == MeetingIdentity()

    def test_the_reverse_pairing_is_a_different_meeting(self) -> None:
        """Home and away is an ORDER, not a set. Both legs resolve."""
        plan = MeetingPlan.from_pairings(E0_LIKE)
        assert isinstance(plan.resolve(("Burnley", "Man City")), MeetingIdentity)
        assert isinstance(plan.resolve(("Man City", "Burnley")), MeetingIdentity)

    def test_identity_uses_the_neutral_ordinal_columns(self) -> None:
        identity = MeetingIdentity()
        assert (identity.stage, identity.leg, identity.replay_number) == (
            "regular",
            1,
            0,
        )

    def test_a_clean_scope_is_fully_resolvable(self) -> None:
        assert MeetingPlan.from_pairings(E0_LIKE).is_fully_resolvable is True
        assert MeetingPlan.from_pairings(E0_LIKE).repeated == {}


class TestRepeatedPairings:
    def test_a_repeated_pairing_refuses(self) -> None:
        plan = MeetingPlan.from_pairings(SC0_LIKE)
        outcome = plan.resolve(("Celtic", "Rangers"))
        assert isinstance(outcome, RepeatedPairing)
        assert outcome.occurrences == 3

    def test_the_refusal_names_no_ordinal(self) -> None:
        """The whole point: there is no stage/leg/replay to read off it."""
        outcome = MeetingPlan.from_pairings(SC0_LIKE).resolve(("Celtic", "Rangers"))
        assert not hasattr(outcome, "stage")
        assert not hasattr(outcome, "replay_number")

    def test_one_repeat_does_not_contaminate_the_clean_pairings(self) -> None:
        plan = MeetingPlan.from_pairings(SC0_LIKE)
        assert isinstance(plan.resolve(("Hearts", "Hibernian")), MeetingIdentity)
        assert plan.is_fully_resolvable is False
        assert plan.repeated == {("Celtic", "Rangers"): 3}

    def test_an_absent_pairing_refuses_with_the_true_reason(self) -> None:
        outcome = MeetingPlan.from_pairings(E0_LIKE).resolve(("Celtic", "Rangers"))
        assert isinstance(outcome, RepeatedPairing)
        assert outcome.occurrences == 0
        assert "not present" in outcome.reason

    def test_two_meetings_is_already_too_many(self) -> None:
        """A two-round league still needs an ordinal we were not given."""
        plan = MeetingPlan.from_pairings([("A", "B"), ("A", "B")])
        assert isinstance(plan.resolve(("A", "B")), RepeatedPairing)
