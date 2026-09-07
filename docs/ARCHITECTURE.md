# Football Prediction Platform — Technical Architecture

Status: draft v1 · Target repo: `tolanifalo/football-prediction-platform` (currently empty)
Stack assumption: Next.js + TypeScript + PostgreSQL/Supabase (app) · Python (statistical modelling)
Data provider: **deliberately undecided** — the architecture is built so this can be chosen late and changed later.

---

## 0. Three decisions that shape everything else

Read these first; the rest of the document is consequence.

**D1 — Separate by module, deploy as three things.**
The brief requires the frontend, football data layer, prediction engine, market/value engine and user app to be *separate*. Separation is enforced by **package boundaries, database grants and typed contracts**, not by five deployed services. Day one ships three deployables: the Next.js app, one Python container (jobs + optional internal API), and Postgres. Microservices are an upgrade path, not a starting point — the split triggers are named in §8.

**D2 — The database is the interface between the engine and the app.**
Predictions are **precomputed and written to Postgres**. The web app never calls the model in a request path. No RPC layer, no read-path queue, trivially cacheable, and the engine can be rewritten in any language without the app noticing. This single decision removes most of the operational complexity a platform like this usually carries.

**D3 — One scoreline distribution is the source of truth for every market.**
Do not model 1X2, over/under, BTTS, double chance, Asian handicap and correct score separately — they will contradict each other. Model goals once (λ_home, λ_away, correlation ρ), derive an 11×11 scoreline matrix, and derive every market from that matrix by summation. Coherent by construction, and far less code.

---

## 1. Recommended architecture

### Runtime topology

```
                       ┌───────────────────────────────┐
   Data providers ───► │  INGEST (Python)              │
   (undecided)         │  adapters/*  →  raw_payloads  │
                       │             →  canonical      │
                       └──────────────┬────────────────┘
                                      │ writes
                       ┌──────────────▼────────────────┐
                       │  POSTGRES / SUPABASE          │◄── writes ── PREDICTION ENGINE (Python)
                       │  canonical · derived ·        │
                       │  predictions · odds · users   │◄── writes ── VALUE ENGINE (Python)
                       └──────────────┬────────────────┘
                                      │ reads only (app_rw role)
                       ┌──────────────▼────────────────┐
                       │  NEXT.JS APP (Vercel)         │
                       │  RSC pages + /api/v1 handlers │
                       │  auth · slips · billing       │
                       └──────────────┬────────────────┘
                                      │
                                   Browser
```

### Logical layers (enforced, non-negotiable)

| Layer | Owns | Must not |
|---|---|---|
| **Ingest** | Provider adapters, raw payload archive, canonical mapping, entity resolution | Contain any modelling logic |
| **Core data** | Competitions, teams, fixtures, results, match stats — provider-neutral facts | Store provider-shaped data, or provider IDs as primary keys |
| **Feature layer** | Team ratings, form windows, xG aggregates, point-in-time feature snapshots | Read any fact whose `known_at` is after the data cutoff |
| **Prediction engine** | Ratings → λ → scoreline matrix → calibrated market probabilities | Read bookmaker odds — ever (see §10, risk 3) |
| **Market/value engine** | Odds ingest, de-vigging, consensus fair price, edge / EV / Kelly | Feed anything back into the prediction engine |
| **Application** | Users, entitlements, slips, presentation, caching | Recompute predictions, or gate premium content client-side |

### Why this shape

- **Provider-agnostic by design.** Every provider hits one adapter interface and lands in `raw_payloads` before anything else happens. You can swap providers, run two in parallel to cross-validate, or re-derive the entire warehouse from archived payloads without re-paying for API calls.
- **Honest by design.** Predictions are append-only and stamped with model version, feature snapshot and data cutoff. Nothing is recomputed in place, so backtests and the public calibration page tell the truth.
- **Cheap by design.** Vercel + Supabase + one small container. Data licensing will be the largest line item long before infrastructure is.

---

## 2. Repository structure

Single monorepo. pnpm workspaces + Turborepo for TypeScript, uv (or Poetry) for Python.

```
football-prediction-platform/
├── apps/
│   ├── web/                        # Next.js App Router
│   │   ├── app/
│   │   │   ├── (public)/           # today, /match/[id], /league/[slug], /team/[slug]
│   │   │   ├── (account)/          # slips, settings, billing
│   │   │   ├── admin/              # data status, job runs, model dashboard
│   │   │   └── api/v1/             # route handlers
│   │   ├── components/
│   │   ├── lib/                    # db client, auth, entitlements, cache keys
│   │   └── tests/                  # vitest + playwright
│   │
│   └── engine/                     # Python — one image, two process types
│       ├── src/engine/
│       │   ├── ingest/
│       │   │   ├── adapters/       # one module per provider, all implement ProviderAdapter
│       │   │   ├── canonical.py    # provider DTO → canonical entity
│       │   │   └── resolve.py      # team/competition entity resolution + aliases
│       │   ├── features/           # rating builders, form windows, point-in-time snapshots
│       │   ├── models/             # elo.py, dixon_coles.py, xg.py, calibration.py
│       │   ├── markets/            # matrix → 1X2 / OU / BTTS / DC / AH / CS  (pure, no I/O)
│       │   ├── value/              # devig.py, consensus.py, edge.py, kelly.py
│       │   ├── evaluation/         # backtest, logloss/brier, calibration curves, CLV
│       │   ├── jobs/               # one entrypoint per scheduled job
│       │   ├── api/                # FastAPI — internal only (see §4D; skip until Phase 4)
│       │   └── db.py               # SQLAlchemy Core; reads the schema, does not own it
│       ├── notebooks/              # research only, never imported by src/
│       ├── tests/
│       └── Dockerfile
│
├── packages/
│   ├── db/                         # Drizzle schema + migrations — SINGLE OWNER of the schema
│   ├── contracts/                  # zod schemas + TS types; JSON-Schema export → pydantic
│   └── config/                     # tsconfig, eslint, tailwind presets
│
├── docs/
│   ├── ARCHITECTURE.md             # this file
│   ├── MODEL.md                    # methodology, assumptions, known limitations
│   └── DATA_PROVIDERS.md           # evaluation matrix, filled in at Phase 1
│
├── infra/                          # fly.toml / railway.json, GH Actions, seed scripts
└── turbo.json · pnpm-workspace.yaml
```

**Schema ownership (a real trap).** Two migration tools fighting over one database is a guaranteed outage. **Drizzle in `packages/db` owns the schema and every migration.** Python reads and writes through SQLAlchemy Core against the existing schema and never migrates. CI regenerates the Python table definitions from the Drizzle schema, so drift fails the build.

> **DEFERRED / NOT YET IMPLEMENTED** — the final sentence above (CI regenerating Python table definitions from the Drizzle schema, with drift failing the build) describes an intended capability that does not exist. No Phase 0 task builds it, and it is **outside current Phase 0 scope**. Until it is built, TypeScript↔Python schema agreement is unverified and must not be assumed. The preceding sentences — Drizzle as sole schema owner, Python never migrating — are in force today.

**Deliberate deferrals:** `packages/ui` — don't create it until a second frontend exists; components live in `apps/web/components` until then. `packages/contracts` — inline the zod schemas in `apps/web` until Python actually needs to consume them (Phase 2).

---

## 3. Database entities

Grouped by owning layer. `timestamptz` everywhere, stored UTC.

### 3.1 Provider abstraction — build this before anything else

| Entity | Key columns | Notes |
|---|---|---|
| `data_sources` | id, slug, kind (`fixtures`/`odds`/`stats`), base_url, rate_limit, active | Provider registry |
| `external_ids` | source_id, entity_type, external_id, internal_id · UNIQUE(source_id, entity_type, external_id) | **One polymorphic table, not one per entity.** Provider IDs never become primary keys |
| `raw_payloads` | source_id, endpoint, params, body `jsonb`, body_hash, fetched_at | Append-only landing zone, partitioned monthly. Lets you re-derive everything without re-fetching |
| `ingestion_runs` | source_id, job, started_at, finished_at, status, rows_in/out, error | Feeds the admin status page |

### 3.2 Canonical football data

| Entity | Key columns |
|---|---|
| `countries` | id, iso_code, name |
| `competitions` | id, slug, country_id, name, type (`league`/`cup`/`international`), tier, is_active |
| `seasons` | id, competition_id, label, start_date, end_date, is_current |
| `teams` | id, slug, canonical_name, short_name, country_id, crest_url, founded |
| `team_aliases` | team_id, alias, source_id, confidence · **required** for entity resolution |
| `venues` | id, name, city, country_id, capacity, latitude, longitude |
| `fixtures` | id, competition_id, season_id, matchweek, home_team_id, away_team_id, kickoff_utc, status (`scheduled`/`live`/`ft`/`postponed`/`abandoned`), venue_id, updated_at · UNIQUE(season_id, home_team_id, away_team_id, kickoff_utc) |
| `match_results` | fixture_id, ht_home, ht_away, ft_home, ft_away, aet_home, aet_away, pens_home, pens_away, settled_at |
| `match_stats` | fixture_id, team_id, is_home, shots, shots_on_target, corners, yellow, red, possession, xg, xga, deep_completions · one row per team per match |
| `match_events` | fixture_id, minute, type, team_id, player_id · Phase 8 (live) and time-decayed models |
| `standings` | season_id, team_id, played, w/d/l, gf, ga, points, position · materialised view, refreshed after results |

### 3.3 Derived / model inputs

| Entity | Key columns | Notes |
|---|---|---|
| `team_ratings` | team_id, competition_id, as_of, rating_type (`elo`/`attack`/`defence`/`xg_attack`/`xg_defence`), value, model_version_id | **Append-only time series.** Never updated |
| `team_form_snapshots` | team_id, as_of, window (5/10/season), venue_split, ppg, gf, ga, xg_for, xg_against, clean_sheets |
| `feature_snapshots` | fixture_id, model_version_id, features `jsonb`, computed_at, **data_cutoff** | Exactly what the model saw. This row is the entire defence against look-ahead leakage |

### 3.4 Predictions

| Entity | Key columns | Notes |
|---|---|---|
| `model_versions` | id, name, semver, algorithm, git_sha, trained_at, training_window, params `jsonb`, status (`shadow`/`active`/`retired`) | **Immutable.** Retraining creates a new row |
| `predictions` | id, fixture_id, model_version_id, computed_at, data_cutoff, lambda_home, lambda_away, rho, xg_home, xg_away, scoreline_matrix `jsonb`, is_current | One row per fixture per model version per run. Append-only; `is_current` flips |
| `prediction_markets` | prediction_id, market_type, line, selection, probability, fair_odds, calibrated | **One table covers every market** via (market_type, line, selection): `1x2/·/home`, `over_under/2.5/over`, `btts/·/yes`, `asian_handicap/-0.5/home`, `correct_score/·/2-1` |

### 3.5 Odds, market and value

| Entity | Key columns | Notes |
|---|---|---|
| `bookmakers` | id, slug, name, country_scope, is_sharp | Flag sharp books — they carry the signal |
| `odds_snapshots` | fixture_id, bookmaker_id, market_type, line, selection, price, captured_at, snapshot_kind (`opening`/`interval`/`closing`) | Partitioned monthly. The volume driver |
| `market_consensus` | fixture_id, market_type, line, selection, fair_prob, overround, method (`multiplicative`/`shin`/`power`), n_books, computed_at | De-vigged market truth |
| `value_signals` | prediction_id, market_type, line, selection, model_prob, market_prob, best_price, bookmaker_id, edge_pct, ev, kelly_fraction, confidence, computed_at | Powers the value board |

### 3.6 Evaluation

| Entity | Key columns |
|---|---|
| `prediction_outcomes` | prediction_market_id, outcome (`win`/`lose`/`push`/`void`), settled_at, closing_price |
| `model_performance` | model_version_id, competition_id, market_type, period_start, period_end, n, log_loss, brier, accuracy, roi_at_close, clv, calibration_bins `jsonb` |

### 3.7 Application

| Entity | Key columns | Notes |
|---|---|---|
| `profiles` | id → `auth.users`, display_name, timezone, odds_format, favourite_competition_ids |
| `subscriptions` | user_id, plan, status, provider (`stripe`), provider_customer_id, provider_subscription_id, current_period_end |
| `entitlements` | user_id, feature_key, granted_until | Resolved server-side; one place to check |
| `slips` | id, user_id, name, status (`draft`/`placed`/`settled`), stake, currency, created_at |
| `slip_selections` | slip_id, fixture_id, market_type, line, selection, price_taken, bookmaker_id, model_prob_at_add | Snapshot the price and probability at add time — never re-read them later |
| `slip_results` | slip_id, outcome, returns, settled_at |
| `follows` | user_id, entity_type (`team`/`competition`), entity_id |
| `job_runs` | job_name, scope_key, run_date, status, started_at, finished_at, error · UNIQUE(job_name, scope_key, run_date) → idempotency |

### Schema rules that matter

1. **Append-only for ratings, predictions, odds and performance.** Corrections are new rows. This is what makes backtesting honest and the public performance page defensible.
2. **`known_at` on every ingested fact** — not `created_at`, but the moment the information became knowable. Everything in §5 depends on it.
3. **Partition `raw_payloads` and `odds_snapshots` by month.** They will be ~95% of your storage.
4. **RLS on all user tables** (`profiles`, `subscriptions`, `slips`, `slip_selections`, `follows`). Public tables: read-only to `anon`.
5. **Premium gating is server-side**, in the API layer. RLS protects user *rows*; it does not hide a premium *field* on a public fixture. Strip those before serialisation.

---

## 4. API boundaries

### A. Provider → Ingest (the seam that keeps providers deferrable)

```
ProviderAdapter (interface)
  list_competitions()               -> [CanonicalCompetition]
  list_fixtures(window, competition)-> [CanonicalFixture]
  fetch_results(since)              -> [CanonicalResult]
  fetch_match_stats(fixture_ids)    -> [CanonicalMatchStats]
  fetch_odds(fixture_ids, markets)  -> [CanonicalOdds]
  capabilities()                    -> {markets, leagues, latency, rate_limit}
```

Rules: adapters return **canonical DTOs only** — a provider shape must never escape `ingest/adapters/`. Every response body is written to `raw_payloads` *before* parsing. Adding a provider is one new file plus one `data_sources` row; nothing else in the system changes.

### B. Engine → Database (write contract, enforced by Postgres grants)

The Python engine writes only: `raw_payloads`, canonical football tables, `team_ratings`, `feature_snapshots`, `predictions`, `prediction_markets`, `odds_snapshots`, `market_consensus`, `value_signals`, `prediction_outcomes`, `model_performance`, `job_runs`.

The web app writes only: `profiles`, `subscriptions`, `entitlements`, `slips`, `slip_selections`, `slip_results`, `follows`.

Neither writes the other's tables. **Enforced by two DB roles with explicit grants**, not by convention — cheap, and it fails loudly.

### C. Web → Client (public API, `/api/v1`)

Server Components read Postgres directly; route handlers exist for client-side interaction and a future mobile client.

```
GET    /api/v1/fixtures?date=&competition=&status=
GET    /api/v1/fixtures/{id}               # full match page payload
GET    /api/v1/competitions
GET    /api/v1/competitions/{slug}/standings
GET    /api/v1/competitions/{slug}/fixtures
GET    /api/v1/teams/{slug}                # profile, form, rating history, upcoming
GET    /api/v1/predictions/today
GET    /api/v1/predictions/history?from=&to=&competition=
GET    /api/v1/value                       # value board                  [premium]
GET    /api/v1/model/performance           # calibration + ROI — keep public, it builds trust
POST   /api/v1/slips
GET    /api/v1/slips/{id}
PATCH  /api/v1/slips/{id}
DELETE /api/v1/slips/{id}
POST   /api/v1/slips/optimize              # the wizard                   [premium]
GET    /api/v1/me
POST   /api/v1/webhooks/stripe
```

Rules: no engine invocation in any request path · responses validated against `packages/contracts` zod schemas · a single `withEntitlements()` guard strips premium fields server-side · public reads use ISR/edge cache keyed on `fixtures.updated_at` · cursor pagination on every list.

### D. Internal engine API (private, small, and *not built in Phase 1*)

```
POST /internal/recompute/{fixture_id}   # admin manual refresh
POST /internal/backtest                 # async, returns run id
GET  /internal/health
```

Private network plus a shared secret. **Skip this entirely until Phase 4** — scheduled job entrypoints cover every Phase 1–3 need. Add it when an admin genuinely needs on-demand recompute.

---

## 5. Prediction-engine boundaries

Each stage is a pure, typed, independently testable function. The engine is **deterministic**: identical `(model_version, feature_snapshot)` must always produce identical output. Seed everything.

```
 [1] FeatureBuilder     (fixture, data_cutoff)        -> FeatureVector
 [2] RatingModel        (match_history)               -> attack / defence / elo per team
 [3] GoalModel          (FeatureVector)               -> (λ_home, λ_away, ρ)
 [4] ScorelineModel     (λ_h, λ_a, ρ)                 -> 11×11 probability matrix
 [5] MarketDerivation   (matrix)                      -> all market probabilities  [pure summation]
 [6] Calibration        (raw_probs, history)          -> calibrated probabilities
 ──────────────────────────── hard wall ────────────────────────────
 [7] ValueEngine        (calibrated_probs, odds)      -> devig → edge, EV, Kelly
 [8] Evaluation         (predictions, outcomes, close)-> log loss, Brier, ROI, CLV, calibration
```

**Stage notes**

- **[1] is the leakage boundary.** The feature builder may only read rows with `known_at <= data_cutoff`. Enforce it in the query layer, not by discipline, and test it (§7).
- **[2]** Fit attack/defence strength per competition, tied together by a shared scale so cross-competition fixtures and promoted teams are handled. Shrink new and promoted teams toward the league prior.
- **[3]** Home advantage, rest days, fixture congestion, travel, xG-based regression to the mean. Keep the feature set small and explainable — an explainable model is a marketable one.
- **[4]** Dixon-Coles low-score correction (or bivariate Poisson). Truncate at 10-10 and renormalise the tail.
- **[5] contains no statistics at all** — it is summation over the matrix. That is precisely why every market agrees with every other market. Keep exactly one implementation, in Python; do not re-derive markets in TypeScript.
- **[6]** Isotonic or Platt scaling, fitted out-of-sample, per market type. Raw model probabilities are almost always overconfident.
- **[7] The hard wall is the most important line in this document.** The prediction engine must never see bookmaker odds. If odds leak into features, "value detection" measures your own reflection — and it will look plausible while being worthless. Enforce by module: `value/` imports from `models/`; `models/` may not import from `value/`. Add an import-linter rule so CI enforces it.
- **[8]** Evaluate against **closing** odds, not the price you happened to see. Beating the close (positive CLV) is the only early evidence the model has real edge.

**Lifecycle:** model versions are immutable. A new version runs in **shadow mode** — predicting live, written to the database, invisible in the UI — for 4–6 weeks, and must beat the incumbent on out-of-sample log loss before promotion to `active`.

---

## 6. Background-job architecture

**Scheduler:** cron inside the engine container (APScheduler or plain crond). Not Airflow, not Celery, not Kafka. A queue earns its place when jobs need fan-out with independent retries at volume — name that trigger, then move.

| Job | Cadence | Purpose |
|---|---|---|
| `sync_competitions_teams` | weekly | Reference data, crests, aliases |
| `sync_fixtures` | every 6h | Next 21 days; detect postponements and kickoff changes |
| `sync_results` | 30 min in match windows, hourly otherwise | Final scores, status transitions |
| `sync_match_stats` | hourly | Shots, xG, corners — arrives late from most providers |
| `rebuild_ratings` | nightly, after results settle | Full rating refresh, append-only |
| `generate_predictions` | nightly + T-3h before kickoff | Late refresh catches team news and line moves |
| `sync_odds` | 60 min at T-72h → 15 min at T-6h → snapshot at kickoff | **Start on day one, before anything consumes it** |
| `compute_value_signals` | after each odds sync | Devig, consensus, edge, EV, Kelly |
| `settle_predictions` | hourly | Resolve outcomes against results |
| `evaluate_models` | nightly | Log loss, Brier, ROI, CLV, calibration bins |
| `refresh_standings` | after results | Materialised view |
| `prune_raw_payloads` | monthly | Drop partitions past retention |
| `live_poller` | 10–30s, Phase 8 | In-play state → Supabase Realtime |

**Cross-cutting rules**

1. **Every job is idempotent**, keyed `UNIQUE(job_name, scope_key, run_date)` in `job_runs`. Re-running is always safe.
2. **One Postgres advisory lock per job name** — overlapping runs are the classic source of duplicate rows.
3. Exponential backoff with jitter; per-provider rate-limit budget tracked in `data_sources`.
4. **A failed job never breaks the site.** The app serves last-known-good data and displays freshness ("odds updated 14 min ago") rather than erroring.
5. Data-quality assertions run as part of every ingest job; a failure quarantines the batch and alerts instead of publishing.

**Closing odds are the single most valuable dataset you will ever collect, and you cannot backfill them.** Capture them from the first week, even with no consumer.

---

## 7. Testing strategy

Weighted toward the numbers rather than the UI — the numbers are where the product's credibility lives.

### Python engine

- **Unit:** market derivation against known-good closed forms (Poisson 1X2 for λ = 1.5 / 1.2 must equal precomputed constants); devig functions must return probabilities summing to exactly 1.
- **Property tests (Hypothesis):** every probability ∈ [0,1] · mutually exclusive outcomes sum to 1 (`P(over 2.5) + P(under 2.5) = 1`, `P(btts yes) + P(btts no) = 1`, 1X2 sums to 1) · the matrix sums to ~1 after renormalisation · **monotonicity** — raising λ_home must never decrease P(home win).
- **Leakage tests — the most important tests in the repository.** Rebuild features for historical fixtures with `data_cutoff = kickoff` and assert that no feature touches a row with `known_at > cutoff`. Failure fails the build.
- **Golden / regression:** snapshot predictions for 100 fixed historical fixtures. Any model change must diff deliberately.
- **Backtest as a CI gate:** a new model version must beat the incumbent on out-of-sample log loss across N seasons, or CI flags the PR.

### TypeScript

- **Unit:** the entitlement guard · slip combination and EV maths · odds format conversion (decimal / fractional / American — a cheap place to look incompetent).
- **Integration:** API route handlers against real Postgres (Testcontainers or a Supabase branch) with seeded fixtures.
- **Contract:** every response validated against its zod schema; schema drift fails CI.
- **RLS:** assert with real anon and authenticated clients that user A cannot read user B's slips.
- **E2E (Playwright) — eight flows, no more:** today's predictions · match page · league page · sign-up · save slip · paywall blocks premium · subscribe · slip optimizer.

### Data quality (runs in production, not only in CI)

After every ingest: no `scheduled` fixture with a kickoff more than 24h in the past · no duplicate `(season, home, away, date)` · every alias resolves to exactly one canonical team · odds within 1.01–1000 · implied overround between 1.00 and 1.30 · no result without a corresponding fixture. Violations quarantine the batch and alert.

**Explicitly not doing:** visual regression, exhaustive component tests, coverage-percentage targets.

---

## 8. Deployment architecture

| Component | Platform | Notes |
|---|---|---|
| Web | **Vercel** | Next.js App Router, preview deploy per PR, ISR + edge cache for public pages |
| Database | **Supabase Postgres** | Managed backups + PITR, Supavisor pooling, Auth, Storage (crests), Realtime (Phase 8) |
| Engine | **Fly.io or Railway**, one container | Two process types from one image: `worker` (scheduler + jobs) and `api` (FastAPI, Phase 4+) |
| Model artifacts | Supabase Storage / S3 | Versioned by `model_version`; the engine loads by version at startup |
| Monitoring | Sentry (both) + structured logs | Public `/status` page driven by `job_runs` and data freshness |
| Analytics | Metabase on a read replica | Model performance, calibration, CLV |

**Environments:** `dev` (local Docker Postgres plus a seeded historical snapshot — never hit providers from a laptop) · `staging` (Supabase branch, real ingest at reduced cadence) · `prod`.

**Database roles:** `app_rw` (web — no access to engine write tables) · `engine_rw` · `analytics_ro`. Grants enforce §4B.

**Migrations:** Drizzle only, run in CI on merge, **expand-then-contract** so app and engine deploys are never coupled.

**Secrets:** provider API keys exist only in the engine environment. They never reach Vercel, and never the client bundle.

**Cost:** Vercel + Supabase + one small Fly machine sits under roughly $50/month until real traffic arrives. Data licensing will dominate the budget.

**Deliberately not deployed:** Kubernetes, separate microservices, Kafka, a feature store, Airflow, a bespoke WebSocket server.

**Split triggers (when to break the monolith):** ingest exceeds the container's rate-limit budget → split ingest workers · live polling starves batch jobs → separate `live` process type · a second frontend (mobile) appears → promote `packages/contracts` to a properly published contract. Until one of these fires, do not split.

---

## 9. Development phases

| Phase | Scope | Est. | Ships |
|---|---|---|---|
| **0 — Foundations** | Monorepo, schema v1, migrations, CI, one league of seeded history, local dev DB | 1–2 wks | Nothing user-facing |
| **1 — Data layer** | `ProviderAdapter` interface + first adapter, ingest jobs, entity resolution, data-quality checks, admin status page. **Begin odds capture now** | 2–3 wks | Admin only |
| **2 — Prediction engine v1** | Ratings, Dixon-Coles, market derivation, backtest harness, calibration, predictions written to DB. No UI | 3–4 wks | Nothing user-facing |
| **3 — Public app** | Today's predictions, match pages, league pages, team pages, prediction history. Read-only, no auth | 3–4 wks | **First real product — ship it** |
| **4 — Odds & value** | Odds at scale, devig, consensus, value signals, value board, model-vs-market page | 2–3 wks | The differentiator |
| **5 — Accounts & premium** | Supabase Auth, profiles, saved slips, Stripe, entitlements, paywall | 2–3 wks | Revenue |
| **6 — Slip optimizer** | Correlation-aware combination scoring, EV/Kelly, constraints (max legs, min odds, leagues) | 2 wks | The "wizard" |
| **7 — Model maturity** | Shadow deploys, per-competition models, xG features, public calibration page | ongoing | Trust |
| **8 — Live** | Live poller, in-play state, Realtime push, in-play model | 4+ wks | Last — highest cost, lowest early payoff |

**Phase 3 is the shipping line.** Everything after it is additive. The most common way a project like this dies is building phases 4–6 before phase 3 has ever been seen by a user.

---

## 10. Major technical risks

Ranked by likelihood × damage.

**1. Data provider risk — cost, coverage, licensing, disappearance.**
Odds redistribution is contractually restricted by several providers; coverage of lower leagues is exactly where edge lives and where data is worst; providers change schemas without notice and shut down.
*Mitigation:* the adapter interface, `raw_payloads` archive and `external_ids` table make swapping or dual-sourcing a one-file change. **Never let a provider ID become a primary key.** Fill in `docs/DATA_PROVIDERS.md` before committing money.

**2. Look-ahead leakage — the silent killer.**
Backtests look brilliant, production is mediocre, and you will not know why for months.
*Mitigation:* `known_at` on every fact, point-in-time `feature_snapshots`, leakage tests as a CI gate, evaluation against closing odds only.

**3. Circular value detection.**
If odds ever reach the model — even as a feature "for accuracy" — edge becomes a measurement of your own reflection, and it will look convincing.
*Mitigation:* the module wall in §5, enforced by an import-linter rule in CI.

**4. The market is efficient, and beating the close is genuinely hard.**
Top-league 1X2 markets are close to unbeatable. Real edge, if it exists, lives in lower leagues and secondary markets.
*Mitigation:* set expectations in product copy · publish CLV honestly · sell *analysis and workflow*, not promised ROI.

**5. Entity resolution.**
"Man Utd" / "Manchester United" / "Man United U21" / relegated clubs / renamed clubs / reserve teams.
*Mitigation:* `team_aliases` with confidence scores, fuzzy match into a human confirmation queue, and **never silently auto-create a team**.

**6. Odds volume and cost.**
Books × markets × lines × fixtures × snapshots compounds fast.
*Mitigation:* monthly partitions, cadence scaled by time-to-kickoff, full fidelity on opening and closing, thin the middle after 90 days.

**7. Calibration drift.**
Models decay across seasons, rule changes and tactical shifts.
*Mitigation:* rolling retrains, shadow mode, and an automatic alert when rolling Brier degrades against a naive baseline.

**8. Fixture lifecycle chaos.**
Postponements, abandonments, venue changes, kickoff moves and neutral venues quietly corrupt results and settlement.
*Mitigation:* UTC everywhere, an explicit status state machine, a nightly reconciliation job.

**9. Serverless + Postgres connection exhaustion.**
The classic Next.js / Supabase production failure.
*Mitigation:* Supavisor pooling, no long transactions in route handlers, no per-request client construction.

**10. Regulatory and compliance exposure.**
Gambling-adjacent content carries age gating, responsible-gambling obligations, jurisdiction restrictions, affiliate rules and app-store policy. Not optional once you monetise.
*Mitigation:* decide the target jurisdictions **before Phase 5**, not after. This can invalidate the business model, so resolve it early and cheaply.

**11. Cold start.**
Promoted teams and early-season fixtures have almost no data and unstable ratings.
*Mitigation:* hierarchical priors, shrinkage to the league mean, and showing uncertainty in the UI rather than hiding it.

**12. Scope.**
The full feature list is well over a year of solo work.
*Mitigation:* §9 exists for this. Phase 3 ships; everything else waits its turn.

---

## Appendix — open decisions

| # | Decision | Needed by |
|---|---|---|
| A1 | Data provider(s) for fixtures / results / stats | Phase 1 |
| A2 | Odds provider and its redistribution licence terms | Phase 4 |
| A3 | Drizzle vs Prisma (recommendation: **Drizzle** — better SQL fidelity, lighter serverless footprint) | Phase 0 |
| A4 | Target jurisdictions and compliance posture | Before Phase 5 |
| A5 | The free / premium feature line | Phase 5 |
| A6 | Competition coverage at launch (recommendation: **3–5 leagues deep, not 40 shallow**) | Phase 1 |
