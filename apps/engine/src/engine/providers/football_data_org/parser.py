"""football-data.org JSON -> canonical fixture DTOs (UPCOMING-FIXTURES.md §3).

THE PROVIDER'S SHAPE STOPS HERE. Nothing downstream sees a `utcDate`, a
`matchday`, or a numeric provider id in a field that pretends to be canonical.
What leaves this module is `CanonicalFixture` plus `Problem`, and a provider
reference carrying the provider's own key as identity INPUT.

NEVER RAISES FOR A BAD ROW. One malformed match must not lose the other
hundred; it becomes a `Problem` and the run reports `partial`.
"""

from __future__ import annotations

import json
import zoneinfo
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from engine.ingestion.dto import (
    CanonicalFixture,
    CompetitionRef,
    FixtureRef,
    PayloadRef,
    SeasonRef,
    TeamRef,
)
from engine.ingestion.errors import Problem, ProblemKind
from engine.ingestion.meetings import MeetingPlan, RepeatedPairing
from engine.providers.football_data_org.catalog import (
    COMPETITIONS,
    STATUS_MAP,
    SUPPORTED_STAGES,
    season_label,
)


@dataclass
class ParsedFixtures:
    """What one `/v4/competitions/{code}/matches` response yielded."""

    fixtures: list[CanonicalFixture] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    rows_seen: int = 0
    #: Provider match id -> the ordered pairing it described. Kept so the job
    #: can report which provider row produced which canonical fixture without
    #: the provider id escaping into a canonical field.
    provider_ids: dict[str, tuple[str, str]] = field(default_factory=dict)
    #: Provider team id -> the exact name it was published under, for the
    #: identity layer to map through `external_ids` once resolved.
    team_ids: dict[str, str] = field(default_factory=dict)


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _kickoff(raw: str) -> datetime:
    """`utcDate` is ISO-8601 with a trailing Z. Reject anything without one.

    A naive datetime would be silently interpreted as local time somewhere,
    which is the class of bug §7 exists to prevent.
    """
    moment = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if moment.tzinfo is None:
        raise ValueError(f"utcDate {raw!r} carries no UTC offset")
    return moment


def parse_matches(
    content: bytes,
    *,
    code: str,
    payload: PayloadRef,
    known_at: datetime,
) -> ParsedFixtures:
    """Map one matches response. Returns problems rather than raising."""
    meta = COMPETITIONS.get(code)
    if meta is None:
        raise ValueError(f"competition {code!r} is not in the project catalogue")

    out = ParsedFixtures()
    try:
        document = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        out.problems.append(
            Problem(ProblemKind.MALFORMED_PAYLOAD, f"response is not JSON: {exc}")
        )
        return out

    matches = document.get("matches")
    if not isinstance(matches, list):
        out.problems.append(
            Problem(ProblemKind.MALFORMED_PAYLOAD, "response has no `matches` array")
        )
        return out

    zone = zoneinfo.ZoneInfo(meta.local_tz)
    staged: list[tuple[dict[str, Any], CanonicalFixture]] = []

    for raw in matches:
        out.rows_seen += 1
        if not isinstance(raw, dict):
            out.problems.append(
                Problem(ProblemKind.MALFORMED_PAYLOAD, "match entry is not an object")
            )
            continue
        built = _one_match(raw, meta, zone, payload, known_at, out)
        if built is not None:
            staged.append((raw, built))

    # -- meeting identity, decided over the WHOLE response ------------------
    # Whether a pairing repeats is a property of the scope, so it cannot be
    # decided one row at a time. The hook is P0-12's, unchanged.
    plan = MeetingPlan.from_pairings(
        (f.ref.home_team.name, f.ref.away_team.name) for _, f in staged
    )
    for raw, fixture in staged:
        pairing = (fixture.ref.home_team.name, fixture.ref.away_team.name)
        outcome = plan.resolve(pairing)
        if isinstance(outcome, RepeatedPairing):
            # The provider supplies `matchday`, which WOULD distinguish these.
            # It is deliberately not used: see UPCOMING-FIXTURES.md §4.
            out.problems.append(
                Problem(
                    ProblemKind.IDENTITY_AMBIGUOUS,
                    f"{pairing[0]} v {pairing[1]} occurs {outcome.occurrences} "
                    f"times in this response; the canonical meeting ordinal "
                    f"cannot be established without inventing one",
                )
            )
            continue
        out.fixtures.append(fixture)
        match_id = _text(raw.get("id")) or str(raw.get("id"))
        out.provider_ids[match_id] = pairing

    return out


def _one_match(
    raw: dict[str, Any],
    meta: Any,
    zone: zoneinfo.ZoneInfo,
    payload: PayloadRef,
    known_at: datetime,
    out: ParsedFixtures,
) -> CanonicalFixture | None:
    """One match object, or a recorded problem and None."""
    match_id = raw.get("id")
    where = f"match {match_id}" if match_id is not None else "match"

    stage = _text(raw.get("stage"))
    if stage is not None and stage not in SUPPORTED_STAGES:
        out.problems.append(
            Problem(
                ProblemKind.UNSUPPORTED_FEATURE,
                f"{where}: stage {stage!r} implies a knockout structure whose "
                f"leg and replay semantics this phase does not establish",
            )
        )
        return None

    provider_status = _text(raw.get("status"))
    status = STATUS_MAP.get(provider_status or "")
    if status is None:
        out.problems.append(
            Problem(
                ProblemKind.UNSUPPORTED_FEATURE,
                f"{where}: status {provider_status!r} has no canonical "
                f"equivalent and is not mapped to a convenient one",
            )
        )
        return None

    home = raw.get("homeTeam") or {}
    away = raw.get("awayTeam") or {}
    home_name = _text(home.get("name"))
    away_name = _text(away.get("name"))
    if not home_name or not away_name:
        out.problems.append(
            Problem(ProblemKind.SCHEMA_VALIDATION, f"{where}: a side has no name")
        )
        return None
    if home_name == away_name:
        out.problems.append(
            Problem(ProblemKind.SCHEMA_VALIDATION, f"{where}: a team plays itself")
        )
        return None

    utc_date = _text(raw.get("utcDate"))
    if not utc_date:
        out.problems.append(
            Problem(ProblemKind.SCHEMA_VALIDATION, f"{where}: no utcDate")
        )
        return None
    try:
        kickoff = _kickoff(utc_date)
    except ValueError as exc:
        out.problems.append(
            Problem(ProblemKind.SCHEMA_VALIDATION, f"{where}: {exc}")
        )
        return None

    season = raw.get("season") or {}
    start = _text(season.get("startDate"))
    if not start:
        out.problems.append(
            Problem(ProblemKind.SCHEMA_VALIDATION, f"{where}: season has no startDate")
        )
        return None
    try:
        label, _year = season_label(date.fromisoformat(start))
    except ValueError as exc:
        out.problems.append(
            Problem(ProblemKind.SCHEMA_VALIDATION, f"{where}: bad startDate: {exc}")
        )
        return None

    for side in (home, away):
        team_id = side.get("id")
        name = _text(side.get("name"))
        if team_id is not None and name:
            out.team_ids[str(team_id)] = name

    competition = CompetitionRef(
        provider_key=meta.code, name="Premier League", country_name="England"
    )
    try:
        return CanonicalFixture(
            payload_ref=payload,
            known_at=known_at,
            ref=FixtureRef(
                season=SeasonRef(
                    competition=competition,
                    label=label,
                    provider_key=_text(season.get("id")) or str(season.get("id")),
                ),
                # The canonical meeting identity. Confirmed by the meeting hook
                # over the whole response before any of these is emitted.
                stage="regular",
                leg=1,
                replay_number=0,
                home_team=TeamRef(
                    provider_key=str(home.get("id")), name=home_name,
                    country_name="England",
                ),
                away_team=TeamRef(
                    provider_key=str(away.get("id")), name=away_name,
                    country_name="England",
                ),
            ),
            kickoff_utc=kickoff,
            local_date=kickoff.astimezone(zone).date(),
            local_tz=meta.local_tz,
            status=status,  # type: ignore[arg-type]
            is_neutral_venue=False,
        )
    except ValueError as exc:
        out.problems.append(
            Problem(ProblemKind.SCHEMA_VALIDATION, f"{where}: {exc}")
        )
        return None
