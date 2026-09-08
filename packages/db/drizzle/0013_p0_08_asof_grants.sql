-- Custom SQL migration file, put your code below! --

-- P0-08: the two fact as-of wrappers and the append-only grant model
-- (PHASE-0-SPEC.md §13.3, §13.8, §2.2).

-- ===========================================================================
-- 1. AS-OF WRAPPERS (§11.1, consumed here - P0-08 owns neither the mechanism
--    nor its design, per §11.7)
-- ===========================================================================
-- Both DELEGATE to fn_visible_at and never restate the predicate. The P0-06
-- catalog assertion now covers FOUR wrappers and must still find exactly one
-- object defining the visibility comparison; restating it here would fail CI.
--
-- LANGUAGE sql is load-bearing, as in 0008 and 0010: SQL functions inline, so
-- the wrapper plans identically to a hand-written predicate and preserves the
-- index scan. A PL/pgSQL equivalent is an optimisation barrier.
--
-- These are the two halves of the §2.4 asymmetry rule in practice:
--   features / reproducing a prediction -> as_of(:data_cutoff)
--   settlement and evaluation           -> as_of(now())
CREATE FUNCTION match_results_as_of(cutoff timestamptz)
RETURNS SETOF match_results
LANGUAGE sql STABLE AS $$
	SELECT * FROM match_results WHERE fn_visible_at(known_at, superseded_at, cutoff)
$$;
--> statement-breakpoint

-- Statistics revise on their own schedule - xG lands days after a score is
-- final - so match_stats is versioned INDEPENDENTLY of match_results (§2.1).
CREATE FUNCTION match_stats_as_of(cutoff timestamptz)
RETURNS SETOF match_stats
LANGUAGE sql STABLE AS $$
	SELECT * FROM match_stats WHERE fn_visible_at(known_at, superseded_at, cutoff)
$$;
--> statement-breakpoint

-- ===========================================================================
-- 2. GRANTS (§2.2, §13.8)
-- ===========================================================================
-- The append-only rule, enforced by PostgreSQL rather than by review. The
-- engine may append a revision and close the old one. It may NOT rewrite a
-- score, an xG, a provenance column, or is_trainable. This is the §E P0-08
-- acceptance criterion: "an UPDATE on a score column is rejected by the
-- database for engine_rw".
GRANT SELECT, INSERT ON "match_results" TO "engine_rw";--> statement-breakpoint
GRANT UPDATE ("superseded_at") ON "match_results" TO "engine_rw";
--> statement-breakpoint

GRANT SELECT, INSERT ON "match_stats" TO "engine_rw";--> statement-breakpoint
GRANT UPDATE ("superseded_at") ON "match_stats" TO "engine_rw";
--> statement-breakpoint

-- No DELETE and no TRUNCATE to any role, on either table. A corrected result
-- is a new revision, never a deletion; TRUNCATE would additionally bypass
-- every row-level protection (§11.3). Both omissions are deliberate and are
-- asserted by db:verify-facts, since an absent grant leaves no trace to read.

GRANT SELECT ON "match_results", "match_stats" TO "app_rw";--> statement-breakpoint
GRANT SELECT ON "match_results", "match_stats" TO "analytics_ro";
