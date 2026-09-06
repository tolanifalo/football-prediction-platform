# football-prediction-platform

Football prediction platform: statistical match modelling, market/value detection, and a public app.
Stack: Next.js + TypeScript + Supabase/Postgres (app) · Python (modelling) · pnpm + Turborepo monorepo · uv for Python.

## Read before changing anything

| Doc | Authority |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System shape, layers, API boundaries, jobs, deployment, phases. **§3 (schema) is superseded — see below.** |
| [docs/DECISIONS-01.md](docs/DECISIONS-01.md) | ORM decision, provider evaluation framework, data-ownership analysis, v1 stress test. **§D (Phase 0 order) is superseded.** |
| [docs/PHASE-0-SPEC.md](docs/PHASE-0-SPEC.md) | **Authoritative for schema, provenance, bitemporality, odds model, reproducibility, and the Phase 0 plan.** |

Where the documents disagree, **PHASE-0-SPEC wins**. It supersedes `ARCHITECTURE.md` §3 and `DECISIONS-01.md` §D. Known divergences are listed in `docs/PHASE-0-SPEC.md`; do not "fix" ARCHITECTURE.md to match without being asked.

## Non-negotiables

These are expensive or impossible to retrofit. Do not relax one without an explicit decision recorded in `docs/`.

1. **The prediction engine never reads bookmaker odds.** `value/` may import `models/`; `models/` may not import `value/`. Otherwise value detection is circular.
2. **Features only read facts with `known_at <= data_cutoff`**, via the `as_of` SQL function. Never hand-write that predicate — every future leakage bug will be a hand-written variant of it.
3. **Fact tables are append-only.** Corrections insert a new revision and set `superseded_at`. Enforced by column-level GRANT, not convention.
4. **Fixture identity never contains kickoff time** — `(season_id, stage, leg, replay_number, home_team_id, away_team_id)`. Rescheduling must not create a duplicate.
5. **`teams` has no name column.** Names live in `team_names` with validity ranges.
6. **Every fact row carries `source_id`, `raw_payload_id`, `known_at`.** No exceptions.
7. **A price is only "closing" if the source defines it as closing.** Our own last observation is `last_observed_pre_kickoff` and is not the same thing.
8. **Odds ticks are written only on change**, never per poll.
9. **The database is the engine↔app interface.** The web app never invokes the model in a request path.
10. **One scoreline matrix derives every market.** Markets are never modelled separately.

## Current phase

**Phase 0** — see `docs/PHASE-0-SPEC.md` §D for the task list and §E for acceptance criteria.

Phase 0 delivers a trustworthy database with proven provenance and **nothing visible**. Do not build: the frontend, the prediction model, the bet-slip wizard, auth/billing, the value engine, live/in-play, or any paid provider integration. The full exclusion list is §G — it is a hard boundary, not a suggestion.

## Commands

```bash
pnpm install && uv sync          # setup
pnpm -r typecheck && pnpm -r lint
uv run pytest -q
uv run ruff check . && uv run mypy src
```

`docs/PHASE-0-SPEC.md` §F lists the full verification set as tasks land.

## Conventions

- All timestamps `timestamptz`, stored UTC. Reject any ingested datetime lacking an explicit UTC offset.
- Migrations: `drizzle-kit generate` + `migrate` only. **Never `drizzle-kit push`** — it silently skips RLS policies.
- Partitioned tables and materialised views are owned by `--custom` SQL migrations and excluded via `tablesFilter`.
- Every feature and training query needs an explicit `ORDER BY` — unordered rows change floating-point summation order and break reproducibility.
