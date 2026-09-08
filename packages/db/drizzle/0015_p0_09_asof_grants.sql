-- Custom SQL migration file, put your code below! --

-- P0-09: the odds as-of wrapper and the odds-layer grant model
-- (PHASE-0-SPEC.md §14.5, §14.8).

-- ===========================================================================
-- 1. AS-OF WRAPPER (§11.1, consumed here - P0-09 owns neither the mechanism
--    nor its design, per §11.7)
-- ===========================================================================
-- The FIFTH wrapper. It DELEGATES to fn_visible_at and never restates the
-- predicate; the P0-06 catalog assertion must still find exactly one object
-- defining the visibility comparison, so restating it here fails CI by design.
--
-- LANGUAGE sql is load-bearing, as in 0008, 0010 and 0013: SQL functions
-- inline, so the wrapper plans identically to a hand-written predicate and
-- preserves the index scan. PL/pgSQL is an optimisation barrier.
--
-- THIS WRAPPER BOUNDS TRANSACTION TIME ONLY. It answers "what did we KNOW by
-- T". It does NOT bound observation time, and a reproducibility read must add
-- `observed_at <= T` and `price_kind = 'observed'` itself:
--
--   SELECT DISTINCT ON (t.series_id) t.series_id, t.price, t.observed_at
--     FROM odds_ticks_as_of(:T) t                 -- what we KNEW at T
--     JOIN odds_series s ON s.id = t.series_id
--    WHERE s.fixture_id = :f
--      AND t.observed_at <= :T                    -- what the market had DONE by T
--      AND t.price_kind  = 'observed'             -- substantiated instants only
--    ORDER BY t.series_id, t.observed_at DESC;
--
-- All three filters are load-bearing and each omission is a different class of
-- leak. provider_at is never used for cutoffs: it is provider-supplied and
-- explicitly untrusted (§14.4).
CREATE FUNCTION odds_ticks_as_of(cutoff timestamptz)
RETURNS SETOF odds_ticks
LANGUAGE sql STABLE AS $$
	SELECT * FROM odds_ticks WHERE fn_visible_at(known_at, superseded_at, cutoff)
$$;
--> statement-breakpoint

-- ===========================================================================
-- 2. GRANTS (§14.8)
-- ===========================================================================
-- bookmakers is plain mutable reference data (§2.1), like countries and
-- venues: a rename is cosmetic. No DELETE - a bookmaker anchors price series.
GRANT SELECT, INSERT, UPDATE ON "bookmakers" TO "engine_rw";
--> statement-breakpoint

-- odds_series is IMMUTABLE AFTER INSERT (§14.3). No UPDATE at all: repointing
-- fixture_id would silently re-attribute an entire price history to a
-- different match. A mis-mapping is corrected by superseding the wrong
-- series' ticks, never by editing the series.
GRANT SELECT, INSERT ON "odds_series" TO "engine_rw";
--> statement-breakpoint

-- odds_ticks is append-only. The engine may record an observation and retract
-- a mis-parse by closing it. It may NOT rewrite a price, an availability flag,
-- a price_kind, any timestamp, or any provenance column.
GRANT SELECT, INSERT ON "odds_ticks" TO "engine_rw";--> statement-breakpoint
GRANT UPDATE ("superseded_at") ON "odds_ticks" TO "engine_rw";
--> statement-breakpoint

-- No DELETE and no TRUNCATE to any role, on any of the three tables. A wrong
-- price is retracted with superseded_at, never removed; TRUNCATE would
-- additionally bypass every row-level protection (§11.3). Both omissions are
-- deliberate and are asserted by db:verify-odds, since an absent grant leaves
-- no trace to read.

GRANT SELECT ON "bookmakers", "odds_series", "odds_ticks" TO "app_rw";--> statement-breakpoint
GRANT SELECT ON "bookmakers", "odds_series", "odds_ticks" TO "analytics_ro";
