"""Deterministic meeting identity (PHASE-0-SPEC.md §17.5).

A provider hands us an ordered pairing inside a season. Whether that pairing
identifies exactly ONE match depends on the competition format, and the
provider does not tell us which format it is. This module decides only what the
data itself decides:

  - the pairing occurs once in the scope   -> the meeting is identified
  - the pairing occurs more than once      -> REFUSE

The second case needs a meeting ordinal - a stage, leg or replay number -
that football-data.co.uk does not supply. Manufacturing one would attach half
of a Scottish league's repeated meetings to the wrong match, silently and
irreversibly. Verified against the real SC0 2023/24 file: the same ordered
pairing occurs two and three times (§16.1).

NO DATABASE, NO PROVIDER. The rule is about competition format, so it belongs
to neither an adapter nor a repository, and every future provider gets the
same answer from the same code.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Self

#: An ordered (home, away) pair of provider team strings.
Pairing = tuple[str, str]

#: What P0-11 writes for a pairing that occurs once. These are the canonical
#: fixture identity's three ordinal columns (§12.2); a single round-robin
#: meeting needs no ordinal, so they take their neutral values.
SINGLE_MEETING_STAGE: Final[str] = "regular"
SINGLE_MEETING_LEG: Final[int] = 1
SINGLE_MEETING_REPLAY: Final[int] = 0


@dataclass(frozen=True)
class MeetingIdentity:
    """The pairing occurs once: its three ordinal columns are determined."""

    stage: str = SINGLE_MEETING_STAGE
    leg: int = SINGLE_MEETING_LEG
    replay_number: int = SINGLE_MEETING_REPLAY


@dataclass(frozen=True)
class RepeatedPairing:
    """The pairing occurs more than once. NEVER invent an ordinal.

    The caller's only correct move is to refuse the affected records. A
    provider that supplies a round, leg or matchday can resolve this by
    supplying the ordinal explicitly - that is a provider capability, not
    something this module may infer.
    """

    pairing: Pairing
    occurrences: int
    reason: str = "repeated ordered pairing needs an explicit meeting ordinal"


MeetingResolution = MeetingIdentity | RepeatedPairing


class MeetingPlan:
    """Classifies every ordered pairing in one competition-season scope.

    Built once per file, then asked about each pairing. Counting first is what
    makes the answer deterministic: whether a pairing is repeated is a property
    of the whole scope, so it cannot be decided one row at a time.
    """

    def __init__(self, counts: Mapping[Pairing, int]) -> None:
        self._counts: dict[Pairing, int] = dict(counts)

    @classmethod
    def from_pairings(cls, pairings: Iterable[Pairing]) -> Self:
        return cls(Counter(pairings))

    @property
    def repeated(self) -> Mapping[Pairing, int]:
        """Every pairing seen more than once, with its count."""
        return {p: n for p, n in self._counts.items() if n > 1}

    @property
    def is_fully_resolvable(self) -> bool:
        """True when no pairing repeats, so the whole scope can be imported."""
        return not self.repeated

    def resolve(self, pairing: Pairing) -> MeetingResolution:
        """The meeting identity, or a refusal. Never a manufactured ordinal."""
        occurrences = self._counts.get(pairing, 0)
        if occurrences == 1:
            return MeetingIdentity()
        if occurrences == 0:
            # Asked about a pairing this scope never contained. Refusing is
            # still the right answer, but say the true reason rather than
            # reporting it as a repeat.
            return RepeatedPairing(
                pairing=pairing,
                occurrences=0,
                reason="pairing is not present in this competition-season scope",
            )
        return RepeatedPairing(pairing=pairing, occurrences=occurrences)
