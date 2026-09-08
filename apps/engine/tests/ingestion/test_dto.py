"""Canonical DTO validation (P0-10 §15.3).

Every rule asserted here traces to an existing constraint in P0-07, P0-08 or
P0-09. Nothing is invented, and nothing is silently coerced.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from engine.ingestion import (
    FixtureRef,
    TeamRef,
)
from engine.ingestion.dto import (
    BookmakerRef,
    CanonicalSeason,
    CompetitionRef,
    SeasonRef,
)

from .conftest import KNOWN_AT, make_fixture, make_odds, make_result


class TestTemporalValidation:
    def test_naive_known_at_is_rejected(
        self,
        fixture_ref: FixtureRef,
        payload_ref: object,
    ) -> None:
        with pytest.raises(ValidationError, match="explicit UTC offset"):
            make_fixture(fixture_ref, payload_ref, known_at=datetime(2026, 5, 1, 18, 0))  # type: ignore[arg-type]

    def test_naive_kickoff_is_rejected(
        self,
        fixture_ref: FixtureRef,
        payload_ref: object,
    ) -> None:
        with pytest.raises(ValidationError, match="explicit UTC offset"):
            naive = datetime(2026, 5, 1, 14, 0)
            make_fixture(fixture_ref, payload_ref, kickoff_utc=naive)  # type: ignore[arg-type]

    def test_naive_observed_at_is_rejected(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        with pytest.raises(ValidationError, match="explicit UTC offset"):
            naive = datetime(2026, 4, 30, 12, 0)
            make_odds(fixture_ref, payload_ref, sportsbook, observed_at=naive)  # type: ignore[arg-type]

    def test_local_date_disagreeing_with_kickoff_is_rejected(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        """A 23:30Z kickoff in São Paulo is the PREVIOUS local day (§6 rule 11)."""
        with pytest.raises(ValidationError, match="disagrees with kickoff_utc"):
            make_fixture(
                fixture_ref,
                payload_ref,
                kickoff_utc=datetime(2026, 6, 1, 23, 30, tzinfo=UTC),
                local_date=date(2026, 6, 2),
                local_tz="America/Sao_Paulo",
            )

    def test_correct_local_date_is_accepted(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        got = make_fixture(
            fixture_ref,
            payload_ref,
            kickoff_utc=datetime(2026, 6, 1, 23, 30, tzinfo=UTC),
            local_date=date(2026, 6, 1),
            local_tz="America/Sao_Paulo",
        )
        assert got.local_date == date(2026, 6, 1)

    def test_unknown_timezone_is_rejected(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        with pytest.raises(ValidationError, match="not a known IANA zone"):
            make_fixture(fixture_ref, payload_ref, local_tz="Europe/Narnia")


class TestFixtureIdentity:
    def test_home_and_away_must_differ(self) -> None:
        team = TeamRef(name="Everton")
        with pytest.raises(ValidationError, match="must differ"):
            FixtureRef(
                season=SeasonRef(
                    competition=CompetitionRef(name="PL"),
                    label="2025/26",
                ),
                stage="regular",
                home_team=team,
                away_team=team,
            )

    def test_leg_outside_one_or_two_is_rejected(self, fixture_ref: FixtureRef) -> None:
        with pytest.raises(ValidationError):
            fixture_ref.model_copy(update={"leg": 3}).model_validate(
                fixture_ref.model_dump() | {"leg": 3}
            )

    def test_provider_key_is_never_called_id(self) -> None:
        """Repositories must not be able to mistake a provider string for a UUID."""
        assert "id" not in TeamRef.model_fields
        assert "provider_key" in TeamRef.model_fields

    def test_unknown_field_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TeamRef(name="Everton", external_id="12345")  # type: ignore[call-arg]

    def test_strict_mode_refuses_to_coerce(self) -> None:
        """A provider sending "3" where an int belongs must fail, not coerce."""
        with pytest.raises(ValidationError):
            CanonicalSeason(
                payload_ref={"body_hash": "a" * 64, "request_signature": "s"},  # type: ignore[arg-type]
                known_at=KNOWN_AT,
                ref=SeasonRef(competition=CompetitionRef(name="PL"), label="2025/26"),
                start_year="2025",  # type: ignore[arg-type]
            )


class TestResultValidation:
    def test_half_time_above_full_time_is_rejected(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        with pytest.raises(ValidationError, match="cannot exceed full-time"):
            make_result(
                fixture_ref,
                payload_ref,
                ft_home=1,
                ft_away=0,
                ht_home=2,
                ht_away=0,
            )

    def test_half_time_pair_must_be_complete(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        with pytest.raises(ValidationError, match="present or absent together"):
            make_result(fixture_ref, payload_ref, ht_home=1)

    def test_extra_time_below_full_time_is_rejected(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        """aet is CUMULATIVE, so it can never be below ft."""
        with pytest.raises(ValidationError, match="cumulative"):
            make_result(
                fixture_ref,
                payload_ref,
                ft_home=2,
                ft_away=2,
                aet_home=1,
                aet_away=2,
            )

    def test_cumulative_extra_time_is_accepted(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        got = make_result(
            fixture_ref,
            payload_ref,
            ft_home=2,
            ft_away=2,
            aet_home=3,
            aet_away=2,
        )
        assert got.aet_home == 3

    def test_shootout_requires_extra_time(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        with pytest.raises(ValidationError, match="requires extra time"):
            make_result(
                fixture_ref,
                payload_ref,
                ft_home=1,
                ft_away=1,
                pens_home=4,
                pens_away=3,
            )

    def test_drawn_shootout_is_rejected(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        with pytest.raises(ValidationError, match="cannot end level"):
            make_result(
                fixture_ref, payload_ref,
                ft_home=1, ft_away=1, aet_home=1, aet_away=1, pens_home=4, pens_away=4,
            )

    def test_awarded_result_can_never_be_trainable(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        with pytest.raises(ValidationError, match="never trainable"):
            make_result(
                fixture_ref,
                payload_ref,
                result_source="awarded",
                is_trainable=True,
            )

    def test_awarded_untrainable_is_accepted(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        got = make_result(
            fixture_ref, payload_ref, result_source="awarded", is_trainable=False,
            ft_home=3, ft_away=0,
        )
        assert got.result_source == "awarded"

    def test_negative_score_is_rejected(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        with pytest.raises(ValidationError):
            make_result(fixture_ref, payload_ref, ft_home=-1)

    def test_goalless_draw_is_a_real_score(
        self, fixture_ref: FixtureRef, payload_ref: object
    ) -> None:
        got = make_result(
            fixture_ref,
            payload_ref,
            ft_home=0,
            ft_away=0,
            ht_home=0,
            ht_away=0,
        )
        assert got.ft_home == 0 and got.ht_home == 0


class TestOddsValidation:
    def test_market_requiring_a_line_must_have_one(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        with pytest.raises(ValidationError, match="requires a line"):
            make_odds(fixture_ref, payload_ref, sportsbook, market_type="over_under",
                      selection="over")

    def test_market_forbidding_a_line_must_not_have_one(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        with pytest.raises(ValidationError, match="must not carry a line"):
            make_odds(fixture_ref, payload_ref, sportsbook, line=Decimal("2.5"))

    def test_over_under_with_a_line_is_accepted(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        got = make_odds(
            fixture_ref, payload_ref, sportsbook,
            market_type="over_under", selection="over", line=Decimal("2.50"),
        )
        assert got.line == Decimal("2.50")

    def test_price_below_the_floor_is_rejected(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        with pytest.raises(ValidationError, match="between 1.01 and 100000"):
            make_odds(fixture_ref, payload_ref, sportsbook, price=Decimal("0.99"))

    def test_suspended_market_must_carry_no_price(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        with pytest.raises(ValidationError, match="suspended market carries no price"):
            make_odds(fixture_ref, payload_ref, sportsbook, is_available=False,
                      price=Decimal("2.10"))

    def test_suspension_with_no_price_is_accepted(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        got = make_odds(
            fixture_ref,
            payload_ref,
            sportsbook,
            is_available=False,
            price=None,
        )
        assert got.price is None

    def test_sportsbook_cannot_offer_a_lay_price(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        with pytest.raises(ValidationError, match="only an exchange offers lay"):
            make_odds(fixture_ref, payload_ref, sportsbook, side="lay")

    def test_exchange_may_offer_a_lay_price(
        self, fixture_ref: FixtureRef, payload_ref: object, exchange: BookmakerRef
    ) -> None:
        got = make_odds(fixture_ref, payload_ref, exchange, side="lay")
        assert got.side == "lay"

    def test_non_observed_price_kind_requires_a_declared_convention(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        """provider_closing carries no substantiated instant (§14.4)."""
        with pytest.raises(ValidationError, match="observed_at_convention"):
            make_odds(
                fixture_ref,
                payload_ref,
                sportsbook,
                price_kind="provider_closing",
            )

    def test_declared_convention_makes_it_acceptable(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        got = make_odds(
            fixture_ref, payload_ref, sportsbook,
            price_kind="provider_closing",
            observed_at_convention="fixture kickoff instant",
        )
        assert got.observed_at_convention

    def test_unapproved_price_kind_is_rejected(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        with pytest.raises(ValidationError):
            make_odds(fixture_ref, payload_ref, sportsbook, price_kind="closing")

    def test_unapproved_market_type_is_rejected(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        with pytest.raises(ValidationError):
            make_odds(fixture_ref, payload_ref, sportsbook, market_type="corners")

    def test_provider_at_may_disagree_wildly_and_is_retained(
        self, fixture_ref: FixtureRef, payload_ref: object, sportsbook: BookmakerRef
    ) -> None:
        """It is untrusted metadata, not a substitute for observed_at."""
        got = make_odds(
            fixture_ref, payload_ref, sportsbook,
            provider_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        assert got.provider_at is not None and got.provider_at < got.observed_at


class TestBookmakerRef:
    def test_sportsbook_with_a_commission_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="only an exchange"):
            BookmakerRef(
                name="Bet365",
                kind="bookmaker",
                commission_rate=Decimal("0.05"),
            )

    def test_exchange_without_a_commission_is_accepted(self) -> None:
        """Permitted, never required - a rate we do not know stays NULL."""
        assert BookmakerRef(name="Smarkets", kind="exchange").commission_rate is None

    def test_aggregator_is_not_a_bookmaker_kind(self) -> None:
        with pytest.raises(ValidationError):
            BookmakerRef(name="Oddschecker", kind="aggregator")  # type: ignore[arg-type]

    def test_commission_out_of_range_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match=">= 0 and < 1"):
            BookmakerRef(name="X", kind="exchange", commission_rate=Decimal("1.5"))
