"""Canonical DTOs — the provider-neutral shapes that leave an adapter.

PHASE-0-SPEC.md §15.3. Pydantic v2 is used here and ONLY here: it validates
untrusted provider data at the ingestion boundary. It is not an ORM, and
canonical persistence remains explicit psycopg SQL in later phases.

PROVIDER IDS ARE NOT CANONICAL IDS. Every reference carries `provider_key`,
which is identity INPUT for the resolver (P0-12) and is destined for
`external_ids` and nowhere else. No field here is called `id`, so a repository
cannot mistake a provider string for a canonical UUID.

Validation is DOMAIN validation, not shape validation. Every rule below is
traceable to an existing constraint in P0-07, P0-08 or P0-09; nothing is
invented. Models are strict, frozen and reject unknown fields, so malformed
provider data fails loudly rather than being coerced into something plausible.
"""

from __future__ import annotations

import zoneinfo
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Vocabularies, each mirroring a CHECK constraint that already exists.
FixtureStatus = Literal[
    "scheduled", "live", "suspended", "ft", "postponed", "abandoned", "cancelled"
]
ResultSource = Literal["played", "awarded"]
MarketType = Literal["1x2", "over_under", "btts", "asian_handicap"]
MarketPeriod = Literal["ft", "ht", "2h"]
MarketSide = Literal["back", "lay"]
PriceKind = Literal["observed", "provider_opening", "provider_closing", "exchange_sp"]
BookmakerKind = Literal["bookmaker", "exchange"]
#: Markets that take a line, and the two that must not (§14.3).
LINE_MARKETS: frozenset[str] = frozenset({"over_under", "asian_handicap"})

NonEmptyStr = Annotated[str, Field(min_length=1)]


class _Base(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


def _require_aware(value: datetime, field_name: str) -> datetime:
    """Reject any datetime without an explicit UTC offset (§6 rule 11).

    'Store UTC' is not enough: providers publish local wall-clock times with no
    offset, and inferring one is how a whole competition ends up an hour wrong.
    Rejection is the rule, never inference.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{field_name} must carry an explicit UTC offset; got a naive datetime"
        )
    return value


# ---------------------------------------------------------------------------
# Identity references — natural keys plus an optional provider key.
# ---------------------------------------------------------------------------
class ProviderRef(_Base):
    """Identity INPUT for the resolver. Never a canonical UUID.

    `provider_key` is the provider's own primary key when it has one. It may be
    absent: P0-11's source identifies a match by date and team names and has no
    identifiers at all.
    """

    provider_key: str | None = None
    name: NonEmptyStr


class CompetitionRef(ProviderRef):
    country_name: str | None = None


class TeamRef(ProviderRef):
    country_name: str | None = None


class SeasonRef(_Base):
    competition: CompetitionRef
    #: "2023/24" for split-year, "2026" for calendar-year (§10.2).
    label: NonEmptyStr
    provider_key: str | None = None


class FixtureRef(_Base):
    """Mirrors the six-column canonical fixture identity key (§12.2).

    Kickoff time is deliberately absent: identity never contains it, or a
    rescheduled match inserts a duplicate instead of revising.
    """

    season: SeasonRef
    #: Free text, and it MUST distinguish structurally repeated meetings -
    #: three-round leagues host the same pairing twice (§12.2).
    stage: NonEmptyStr
    leg: int = Field(default=1, ge=1, le=2)
    replay_number: int = Field(default=0, ge=0)
    home_team: TeamRef
    away_team: TeamRef
    provider_key: str | None = None

    @model_validator(mode="after")
    def _teams_differ(self) -> Self:
        if self.home_team == self.away_team:
            raise ValueError("home_team and away_team must differ")
        return self


class PayloadRef(_Base):
    """Provenance: which archived bytes this record was parsed from (§1.3)."""

    body_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_signature: NonEmptyStr


class _Sourced(_Base):
    """Every canonical record carries its provenance and knowledge time."""

    payload_ref: PayloadRef
    known_at: datetime

    @model_validator(mode="after")
    def _known_at_is_aware(self) -> Self:
        _require_aware(self.known_at, "known_at")
        return self


# ---------------------------------------------------------------------------
# Canonical records
# ---------------------------------------------------------------------------
class CanonicalCompetition(_Sourced):
    ref: CompetitionRef
    competition_type: Literal[
        "league", "domestic_cup", "super_cup", "continental", "international"
    ]
    gender: Literal["men", "women"]
    age_group: str = "senior"
    tier: int | None = Field(default=None, ge=1)
    confederation: str | None = None


class CanonicalSeason(_Sourced):
    ref: SeasonRef
    start_year: int = Field(ge=1850, le=2200)
    start_date: date | None = None
    end_date: date | None = None

    @model_validator(mode="after")
    def _dates_ordered(self) -> Self:
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date")
        return self


class CanonicalTeam(_Sourced):
    ref: TeamRef
    gender: Literal["men", "women"]
    team_type: Literal["club", "national"] = "club"
    age_group: str = "senior"
    is_reserve: bool = False


class CanonicalFixture(_Sourced):
    """A fixture and the schedule revision observed with it (§12.2, §12.4)."""

    ref: FixtureRef
    kickoff_utc: datetime
    #: Competition-local calendar date, stored not generated (§6 rule 11).
    local_date: date
    local_tz: NonEmptyStr
    status: FixtureStatus
    venue_name: str | None = None
    is_neutral_venue: bool = False

    @model_validator(mode="after")
    def _temporal_rules(self) -> Self:
        _require_aware(self.kickoff_utc, "kickoff_utc")
        try:
            tz = zoneinfo.ZoneInfo(self.local_tz)
        except Exception as exc:  # noqa: BLE001 - every zoneinfo failure means the same
            message = f"local_tz {self.local_tz!r} is not a known IANA zone"
            raise ValueError(message) from exc
        expected = self.kickoff_utc.astimezone(tz).date()
        if expected != self.local_date:
            raise ValueError(
                f"local_date {self.local_date} disagrees with kickoff_utc "
                f"in {self.local_tz} "
                f"(expected {expected})"
            )
        return self


class CanonicalResult(_Sourced):
    """A football result (§13.2). Scores only - never a probability."""

    fixture: FixtureRef
    result_source: ResultSource
    is_trainable: bool
    ft_home: int = Field(ge=0)
    ft_away: int = Field(ge=0)
    ht_home: int | None = Field(default=None, ge=0)
    ht_away: int | None = Field(default=None, ge=0)
    #: CUMULATIVE at the end of extra time, not goals scored during it.
    aet_home: int | None = Field(default=None, ge=0)
    aet_away: int | None = Field(default=None, ge=0)
    pens_home: int | None = Field(default=None, ge=0)
    pens_away: int | None = Field(default=None, ge=0)
    occurred_at: datetime | None = None

    @model_validator(mode="after")
    def _score_rules(self) -> Self:
        if self.occurred_at is not None:
            _require_aware(self.occurred_at, "occurred_at")
        if (self.ht_home is None) != (self.ht_away is None):
            raise ValueError("half-time scores must be present or absent together")
        if self.ht_home is not None and self.ht_away is not None:
            if self.ht_home > self.ft_home or self.ht_away > self.ft_away:
                raise ValueError("half-time score cannot exceed full-time score")
        if (self.aet_home is None) != (self.aet_away is None):
            raise ValueError("extra-time scores must be present or absent together")
        if self.aet_home is not None and self.aet_away is not None:
            if self.aet_home < self.ft_home or self.aet_away < self.ft_away:
                raise ValueError(
                    "extra-time score is cumulative and cannot be "
                    "below the full-time score"
                )
        if (self.pens_home is None) != (self.pens_away is None):
            raise ValueError("shootout scores must be present or absent together")
        if self.pens_home is not None:
            if self.aet_home is None:
                raise ValueError("a shootout requires extra time")
            if self.pens_home == self.pens_away:
                raise ValueError("a shootout cannot end level")
        # An awarded score is an administrative outcome and must never train
        # the goals model (§6 rule 3).
        if self.result_source == "awarded" and self.is_trainable:
            raise ValueError("an awarded result is never trainable")
        return self


class BookmakerRef(ProviderRef):
    """Whose price this is. Distinct from who supplied it (§14.2)."""

    kind: BookmakerKind
    commission_rate: Decimal | None = None

    @model_validator(mode="after")
    def _commission_only_for_exchanges(self) -> Self:
        if self.commission_rate is None:
            return self
        if self.kind != "exchange":
            raise ValueError("only an exchange may declare a commission_rate")
        if not (Decimal(0) <= self.commission_rate < Decimal(1)):
            raise ValueError("commission_rate must be >= 0 and < 1")
        return self


class CanonicalOdds(_Sourced):
    """One market observation (§14.3, §14.4)."""

    fixture: FixtureRef
    bookmaker: BookmakerRef
    period: MarketPeriod = "ft"
    market_type: MarketType
    #: Required exactly for over_under and asian_handicap; forbidden otherwise.
    #: For asian_handicap the line is ALWAYS from the HOME team's perspective.
    line: Decimal | None = None
    selection: NonEmptyStr
    side: MarketSide = "back"
    price: Decimal | None = None
    is_available: bool = True
    price_kind: PriceKind = "observed"
    observed_at: datetime
    provider_at: datetime | None = None
    #: Mandatory for non-`observed` kinds: those carry no substantiated instant,
    #: so the adapter must declare the convention it applied (§14.4).
    observed_at_convention: str | None = None

    @model_validator(mode="after")
    def _market_and_price_rules(self) -> Self:
        _require_aware(self.observed_at, "observed_at")
        if self.provider_at is not None:
            _require_aware(self.provider_at, "provider_at")
        needs_line = self.market_type in LINE_MARKETS
        if needs_line and self.line is None:
            raise ValueError(f"market_type {self.market_type} requires a line")
        if not needs_line and self.line is not None:
            raise ValueError(f"market_type {self.market_type} must not carry a line")
        if not self.is_available and self.price is not None:
            raise ValueError("a suspended market carries no price")
        if self.price is not None and not (
            Decimal("1.01") <= self.price <= Decimal(100000)
        ):
            raise ValueError("price must lie between 1.01 and 100000")
        if self.side == "lay" and self.bookmaker.kind != "exchange":
            raise ValueError("only an exchange offers lay prices")
        if self.price_kind != "observed" and not self.observed_at_convention:
            raise ValueError(
                f"price_kind {self.price_kind} carries no substantiated instant; "
                "the adapter must declare observed_at_convention"
            )
        return self


#: Anything an adapter may return. Repositories dispatch on this union.
CanonicalRecord = (
    CanonicalCompetition
    | CanonicalSeason
    | CanonicalTeam
    | CanonicalFixture
    | CanonicalResult
    | CanonicalOdds
)
