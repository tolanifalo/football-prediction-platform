-- Custom SQL migration file, put your code below! --

-- P0-07: the schedule as-of wrapper and the fixture grant model
-- (PHASE-0-SPEC.md §12.4, §12.6).

-- ===========================================================================
-- 1. THE AS-OF WRAPPER (§11.1, §12.4)
-- ===========================================================================
-- fixture_schedule is the second full-bitemporal table, and this is the first
-- consumer of the mechanism P0-06 built. It DELEGATES to fn_visible_at and
-- never restates the predicate: db:verify-external-ids check 19 asserts that
-- exactly ONE object in `public` contains that logic, so restating it here
-- would fail CI. That is the single-place rule working, not a coincidence.
--
-- LANGUAGE sql is load-bearing, as in 0008: SQL functions are inlined by the
-- planner. Measured on 18,720 seeded revisions - the wrapper produces exactly
-- the plan a hand-written predicate produces, down to the Filter text, and
-- preserves the index scan. A PL/pgSQL equivalent is an optimisation barrier.
CREATE FUNCTION fixture_schedule_as_of(cutoff timestamptz)
RETURNS SETOF fixture_schedule
LANGUAGE sql STABLE AS $$
	SELECT * FROM fixture_schedule WHERE fn_visible_at(known_at, superseded_at, cutoff)
$$;
--> statement-breakpoint

-- ===========================================================================
-- 2. GRANTS (§12.6)
-- ===========================================================================
-- fixtures: identity is immutable by PRIVILEGE, not by convention. engine_rw
-- may write exactly three columns after insert, because exactly three are
-- genuinely learned later: a stats-completeness watermark when late data lands
-- (§6 rule 13), a tie relationship when the draw is understood, and a replay
-- link when the replay is scheduled.
GRANT SELECT, INSERT ON "fixtures" TO "engine_rw";--> statement-breakpoint
GRANT UPDATE ("stats_complete_at", "tie_id", "replaces_fixture_id") ON "fixtures" TO "engine_rw";
--> statement-breakpoint

-- fixture_schedule: the §2.2 bitemporal pattern verbatim. The engine can
-- append a revision and close the old one. It cannot rewrite a kickoff, a
-- venue or a status - PostgreSQL enforces that; no code review can.
GRANT SELECT, INSERT ON "fixture_schedule" TO "engine_rw";--> statement-breakpoint
GRANT UPDATE ("superseded_at") ON "fixture_schedule" TO "engine_rw";
--> statement-breakpoint

-- No DELETE to any role on either table: a fixture anchors results, odds,
-- feature snapshots and predictions, which is the same reasoning that
-- withholds DELETE on competitions and seasons (§10.6). No TRUNCATE anywhere
-- (§11.3). Both omissions are deliberate and are asserted by
-- db:verify-fixtures, since an absent grant leaves no trace to read.

GRANT SELECT ON "fixtures", "fixture_schedule" TO "app_rw";--> statement-breakpoint
GRANT SELECT ON "fixtures", "fixture_schedule" TO "analytics_ro";
