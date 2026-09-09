# Upcoming fixtures — football-data.org

**Phase 1, milestone 7.** One provider-backed vertical slice that supplies *future* Premier League fixtures, so the production prediction job has something to predict. Authoritative for that provider, its identity handling and its limits.

**Zero migrations.** Everything reuses the existing canonical schema: `fixtures`, `fixture_schedule`, `team_aliases`, `external_ids`, `entity_review_queue`, `raw_payloads`, `job_runs`. No provider-specific table, no prediction/market/odds/scheduler table.

## 1. The provider, and why

**football-data.org** — `https://api.football-data.org/v4`.

**This is a different organisation from football-data.co.uk**, despite the near-identical name. `.co.uk` publishes historical CSV archives of *completed* matches and is what the six-season corpus came from. `.org` is a documented JSON API carrying *current and upcoming* fixtures. Both are used, for different jobs. Confusing them would be very easy.

| requirement | football-data.org |
|---|---|
| documented API | yes, versioned (v4), public reference docs |
| stable identifiers | integer `id` per match, per team, per competition, per season |
| upcoming fixtures | yes — the reason for choosing it |
| kickoff date/time | `utcDate`, ISO-8601 with an explicit `Z` |
| competition identity | `code` (`PL`) and numeric `id` (2021) |
| season identity | numeric `id` plus `startDate`/`endDate` |
| postponed/rescheduled | `status` enum incl. `POSTPONED`, `CANCELLED`, `SUSPENDED` |
| repeated pairings | `matchday` distinguishes them — see §4 |
| free access | free tier covers `PL` (`plan: TIER_ONE`); a token is required |

**Verified live, without credentials**, via the open `/v4/competitions` endpoint: competition id **2021**, code `PL`, name "Premier League", area England (id 2072), plan `TIER_ONE`, current season id **2502** running 2026-08-21 → 2027-05-30.

Also verified live: `GET /v4/competitions/PL/matches` with no token returns **403**, and with an invalid token **400** `{"message":"Your API token is invalid."}`. Those two shapes are what the adapter's error mapping is built against.

**Alternatives considered.** *API-Football* — comparable quality, also key-gated, no advantage here. *TheSportsDB* — free but inconsistent coverage and weaker identifiers. *fixturedownload.com* / community CSV mirrors — no documented API, **no stable identifiers**, and chosen only for being easy to scrape, which §2 explicitly rules out.

**No credentials were created and nothing was purchased.** The token is read from `FOOTBALL_DATA_ORG_TOKEN` and injected by the existing transport; it never reaches a request signature, `raw_payloads.request_params`, or any source file. **The entire phase is testable without it**: every test drives checked-in payloads through `httpx.MockTransport`.

## 2. Provider boundary

Nothing new was built. The adapter implements the existing `ProviderAdapter` Protocol and returns `FetchResult(records=[CanonicalFixture], provenance=[PayloadRef], complete=..., problems=[...])`. It is handed an `HttpTransport` and a `RawArchive` and depends on nothing else — a test asserts the package imports no psycopg, no `engine.ingestion.postgres`, and no `engine.model`.

`utcDate`, `matchday` and numeric ids stop at the parser. What leaves is canonical DTOs plus provider keys carried as **identity input** on the refs, never as canonical values.

## 3. Raw evidence

Unchanged rules, unchanged system: exact response bytes archived, content-addressed body, one observation per fetch, deterministic request signature over the endpoint **template** (never an interpolated URL, so a key cannot land in one), recursive secret redaction, the approved response-header whitelist, `job_run_id` attached, adapter version recorded. A 403 is archived exactly like a 200 — bytes are evidence whether or not they are useful.

## 4. Fixture identity — what the provider can and cannot establish

Canonical identity is unchanged: `(season_id, stage, leg, replay_number, home_team_id, away_team_id)`. **Kickoff is not identity**, which is what lets a reschedule be a new schedule revision rather than a new fixture.

| the provider supplies | used for identity? |
|---|---|
| numeric match `id` | **no** — see below |
| `matchday` (1–38) | **no** — see below |
| `stage` (`REGULAR_SEASON`) | yes, as the single accepted stage |
| leg / replay | **not supplied at all** |

**Provider match IDs are not mapped.** `external_ids.entity_type` permits five registries and `fixture` is deliberately not one of them (`PHASE-0-SPEC` §11.4). Mapping fixture IDs would need a migration to that CHECK *and* to the P0-06 integrity trigger, and nothing in this phase requires it: the canonical identity key is what finds the fixture on a re-fetch, so idempotency works without it. The provider id survives in raw evidence. Adding it later is a deliberate schema decision, not a side effect.

**`matchday` is available and deliberately unused.** It *would* distinguish a repeated ordered pairing. Using it would encode `stage` as something like `regular_md5`, which the historical importer does not do — the same match ingested by both providers would then get two different canonical identities and fork the registry. Provider-independence of identity is worth more than resolving a case the Premier League does not produce.

**A repeated ordered pairing is therefore refused**, via the P0-12 `MeetingPlan` hook, unchanged. The refusal names the missing ordinal; the unaffected fixtures in the same response still land. Nothing is invented.

**Unsupported stages are refused.** The provider's enum includes `SEMI_FINALS`, `PLAYOFFS`, `GROUP_STAGE` and more, all of which imply leg and replay semantics this phase does not establish. Only `REGULAR_SEASON` is accepted.

## 5. Identity resolution

The P0-12 split, reused exactly:

- **Competition and season are DECLARED.** The catalogue states `PL` means the English Premier League; the provider supplies the season's dates. The canonical rows are created if absent — a season is a registry entity we derive, not a claim to adjudicate. Both providers produce the same `"2026/27"` label shape, so their fixtures land under one `seasons` row.
- **Teams are RESOLVED**, by exact `external_ids` mapping or exact normalised alias, and nothing else. **No fuzzy matching.** An unknown or ambiguous club is refused, the fixture is skipped, and the name goes to `entity_review_queue`. **No team is ever created.**

Once a club resolves, the provider's numeric team id is recorded in `external_ids` — that *is* a provider primary key, which is what the table is for.

**The alias table is not verified against the live API.** Every endpoint that lists teams needs a token, so the spellings in `seed.py` are the provider's documented naming convention (`"Arsenal FC"`, `"Wolverhampton Wanderers FC"`) rather than bytes anyone has seen. **That is safe only because resolution fails closed**: a wrong spelling resolves to nothing and lands in the review queue. A mistake here is loud, never a silent mis-mapping. These are different strings from the `.co.uk` archive's `"Arsenal"` / `"Wolves"`, which is exactly why `team_aliases.source_id` scopes a spelling to its provider.

## 6. Schedule facts and temporal semantics

Schedules are written through the existing bitemporal `fixture_schedule`: kickoff UTC, competition-local timezone and date, status, neutral-venue flag, provenance. A change **supersedes and inserts**; nothing is overwritten, so a postponement leaves the earlier "scheduled" fact readable at its own `known_at`.

Status mapping: `SCHEDULED`/`TIMED` → `scheduled`, `IN_PLAY`/`PAUSED` → `live`, `SUSPENDED` → `suspended`, `FINISHED` → `ft`, `POSTPONED` → `postponed`, `CANCELLED` → `cancelled`. **`AWARDED` is deliberately unmapped** — calling it `ft` would claim ninety minutes were played and `cancelled` would claim there is no result. It is refused with a problem.

**`known_at` is when we learned, never what the provider claims.** The payload's `lastUpdated` is never backdated into it — a test asserts `known_at` is later than the moment the run began. **Kickoff is never "now"**: it comes from `utcDate`, and a naive datetime is rejected rather than guessed at.

**`as_of` must be taken after the aliases are written.** The first version of the job captured it at job start, before seeding the provider's spellings; `fn_visible_at` then hid the rows the job had just inserted and every club resolved as unknown. Transaction time means *what we believe now*, and "now" is after the write.

## 7. The chain

```
provider response -> canonical fixture -> fixture_schedule
                  -> prediction eligibility -> production prediction
```

Demonstrated end to end in `test_upcoming_fixtures_db.py`: three ingested upcoming fixtures become three eligible fixtures at a `cutoff = now`, and the production job (180-day decay) writes three predictions whose 1X2 sums to 1. **No result is required or consulted** — the upcoming fixtures have no score at all, which is the point.

**Test isolation is by wipe, not rollback**, and that is forced: `run_import` commits, because evidence must be durable before anything is parsed, so a rolled-back transaction cannot contain it. These tests use P0-11's `clean_db`, which empties the canonical tables before and after each one, so no fabricated fixture can survive into the authoritative corpus.

## 8. Known limitations

**A live run needs a token.** Without `FOOTBALL_DATA_ORG_TOKEN` the job exits with a clear message; the adapter and its whole test path run regardless.

**Team spellings are unverified** (§5). The first live run should be treated as a review exercise: whatever the queue reports is the correction list.

**One competition, one domain.** `PL` only, fixtures only. No results, no odds, no multi-league orchestration, no scheduler.

**Free-tier rate limits** are not modelled beyond the existing retry policy; `capabilities()` declares 10 requests/minute as documentation, and nothing enforces it.

**Repeated pairings and knockout stages are refused**, not handled (§4).

**No fixture-level external ID** (§4), so a provider that renumbers its matches cannot be detected as having done so.

---
