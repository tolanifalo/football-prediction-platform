-- Custom SQL migration file, put your code below! --

-- P0-06: the as-of mechanism, polymorphic referential integrity, and grants
-- (PHASE-0-SPEC.md §11.1, §11.3, §11.7).

-- ===========================================================================
-- 1. THE AS-OF MECHANISM (§11.1)
-- ===========================================================================
-- The bitemporal visibility predicate lives HERE AND NOWHERE ELSE. Wrappers
-- delegate to it; verification calls it; application code calls it. Every
-- leakage bug in this system's future is a hand-written variant of this
-- WHERE clause (§2.2), so a CI catalog assertion proves this is the only
-- object that contains it.
--
-- LANGUAGE sql (not plpgsql) is load-bearing: SQL functions are inlined by the
-- planner and preserve index pushdown. A PL/pgSQL equivalent was measured
-- producing an opaque Function Scan that discarded 14,999 rows to return 1.
CREATE FUNCTION fn_visible_at(
	known_at      timestamptz,
	superseded_at timestamptz,
	cutoff        timestamptz
) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
	SELECT known_at <= cutoff AND (superseded_at IS NULL OR superseded_at > cutoff)
$$;
--> statement-breakpoint

-- Thin per-table wrapper. Delegates; never restates the predicate.
-- RETURNS SETOF external_ids preserves full column typing for both drivers.
CREATE FUNCTION external_ids_as_of(cutoff timestamptz)
RETURNS SETOF external_ids
LANGUAGE sql STABLE AS $$
	SELECT * FROM external_ids WHERE fn_visible_at(known_at, superseded_at, cutoff)
$$;
--> statement-breakpoint

-- ===========================================================================
-- 2. POLYMORPHIC REFERENTIAL INTEGRITY (§11.3)
-- ===========================================================================
-- This is TRIGGER-ENFORCED integrity, NOT a PostgreSQL foreign key.
-- information_schema and drizzle-kit will not report it, which is exactly why
-- db:verify-external-ids must assert it explicitly.
--
-- Both sides are mandatory. Proven: with the canonical-side trigger removed,
-- deleting a mapped team succeeds and leaves an orphaned mapping.

-- Mapping side: existence, type validity, and immutability of identity.
CREATE FUNCTION trg_external_ids_integrity() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
	target_table text;
	found_row    boolean;
BEGIN
	IF TG_OP = 'UPDATE' THEN
		-- A remap must supersede and insert, never rewrite (§6 rule 9).
		IF NEW.internal_id IS DISTINCT FROM OLD.internal_id THEN
			RAISE EXCEPTION
				'external_ids.internal_id is immutable; supersede this mapping and insert a new one'
				USING ERRCODE = 'restrict_violation';
		END IF;
		IF NEW.entity_type IS DISTINCT FROM OLD.entity_type THEN
			RAISE EXCEPTION 'external_ids.entity_type is immutable'
				USING ERRCODE = 'restrict_violation';
		END IF;
		RETURN NEW;   -- superseded_at / confidence / last_verified_at may change
	END IF;

	target_table := CASE NEW.entity_type
		WHEN 'country'     THEN 'countries'
		WHEN 'competition' THEN 'competitions'
		WHEN 'season'      THEN 'seasons'
		WHEN 'team'        THEN 'teams'
		WHEN 'venue'       THEN 'venues'
	END;
	IF target_table IS NULL THEN
		RAISE EXCEPTION 'unknown entity_type %', NEW.entity_type
			USING ERRCODE = 'foreign_key_violation';
	END IF;

	-- FOR KEY SHARE, not a bare SELECT. Without the lock a concurrent DELETE of
	-- the canonical row interleaves and leaves an orphan - reproduced, then
	-- shown closed by this lock in both interleavings.
	EXECUTE format('SELECT true FROM %I WHERE id = $1 FOR KEY SHARE', target_table)
		INTO found_row USING NEW.internal_id;

	IF NOT COALESCE(found_row, false) THEN
		RAISE EXCEPTION 'external_ids.internal_id % is not present in % (entity_type=%)',
			NEW.internal_id, target_table, NEW.entity_type
			USING ERRCODE = 'foreign_key_violation';
	END IF;
	RETURN NEW;
END $$;
--> statement-breakpoint

CREATE TRIGGER external_ids_integrity
	BEFORE INSERT OR UPDATE ON external_ids
	FOR EACH ROW EXECUTE FUNCTION trg_external_ids_integrity();
--> statement-breakpoint

-- Canonical side: a mapped entity cannot be deleted. Not redundant with the
-- P0-05 grants: engine_rw genuinely holds DELETE on countries and venues
-- (§10.6), so for those two this trigger is the only guard.
CREATE FUNCTION trg_block_delete_if_mapped() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
	etype     text;
	ref_count integer;
BEGIN
	etype := CASE TG_TABLE_NAME
		WHEN 'countries'    THEN 'country'
		WHEN 'competitions' THEN 'competition'
		WHEN 'seasons'      THEN 'season'
		WHEN 'teams'        THEN 'team'
		WHEN 'venues'       THEN 'venue'
	END;
	SELECT count(*) INTO ref_count
	  FROM external_ids
	 WHERE entity_type = etype AND internal_id = OLD.id;
	IF ref_count > 0 THEN
		RAISE EXCEPTION '% % is still referenced by % external_ids row(s)',
			TG_TABLE_NAME, OLD.id, ref_count
			USING ERRCODE = 'foreign_key_violation';
	END IF;
	RETURN OLD;
END $$;
--> statement-breakpoint

CREATE TRIGGER countries_block_delete_if_mapped    BEFORE DELETE ON countries    FOR EACH ROW EXECUTE FUNCTION trg_block_delete_if_mapped();--> statement-breakpoint
CREATE TRIGGER competitions_block_delete_if_mapped BEFORE DELETE ON competitions FOR EACH ROW EXECUTE FUNCTION trg_block_delete_if_mapped();--> statement-breakpoint
CREATE TRIGGER seasons_block_delete_if_mapped      BEFORE DELETE ON seasons      FOR EACH ROW EXECUTE FUNCTION trg_block_delete_if_mapped();--> statement-breakpoint
CREATE TRIGGER teams_block_delete_if_mapped        BEFORE DELETE ON teams        FOR EACH ROW EXECUTE FUNCTION trg_block_delete_if_mapped();--> statement-breakpoint
CREATE TRIGGER venues_block_delete_if_mapped       BEFORE DELETE ON venues       FOR EACH ROW EXECUTE FUNCTION trg_block_delete_if_mapped();
--> statement-breakpoint

-- ===========================================================================
-- 3. GRANTS (§11.7)
-- ===========================================================================
-- external_ids identity is immutable by privilege as well as by trigger: only
-- the three temporal/verification columns are UPDATE-able. No TRUNCATE is
-- granted anywhere - it bypasses row triggers and would orphan mappings
-- silently (§11.3).
GRANT SELECT, INSERT ON "external_ids" TO "engine_rw";--> statement-breakpoint
GRANT UPDATE ("superseded_at", "confidence", "last_verified_at") ON "external_ids" TO "engine_rw";
--> statement-breakpoint

-- P0-12 populates and resolves review items; P0-06 leaves the table empty.
GRANT SELECT, INSERT ON "entity_review_queue" TO "engine_rw";--> statement-breakpoint
GRANT UPDATE ("status", "resolved_at", "resolved_by", "resolution_note") ON "entity_review_queue" TO "engine_rw";
--> statement-breakpoint

GRANT SELECT ON "external_ids", "entity_review_queue" TO "app_rw";--> statement-breakpoint
GRANT SELECT ON "external_ids", "entity_review_queue" TO "analytics_ro";
