-- Custom SQL migration file, put your code below! --

-- P0-05: the canonical entity layer's only custom SQL (PHASE-0-SPEC.md §10.4).
-- Structure needed no raw SQL: partial unique indexes cover every temporal
-- guarantee, so there is no btree_gist and no exclusion constraint.
--
-- Grant model (§10.6):
--   engine_rw    ingests and corrects canonical data, but may never rewrite a
--                historical name and may never delete a team.
--   app_rw       reads canonical data only. It writes application tables,
--                which do not exist yet (Phase 5).
--   analytics_ro read-only across the layer.
--
-- NOTE: P0-04 created engine_rw. This adds app_rw and analytics_ro, which
-- ARCHITECTURE.md §8 and §D otherwise assign to P0-16; they are created here
-- because P0-05's grant model is specified in terms of all three. P0-16 still
-- owns RLS and the application-table grants.

DO $$
BEGIN
	IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_rw') THEN
		CREATE ROLE "app_rw" NOLOGIN;
	END IF;
	IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'analytics_ro') THEN
		CREATE ROLE "analytics_ro" NOLOGIN;
	END IF;
END
$$;
--> statement-breakpoint

GRANT USAGE ON SCHEMA "public" TO "app_rw", "analytics_ro";
--> statement-breakpoint

-- Plain mutable reference data (§2.1): fully correctable.
GRANT SELECT, INSERT, UPDATE, DELETE ON "countries" TO "engine_rw";--> statement-breakpoint
GRANT SELECT, INSERT, UPDATE, DELETE ON "venues"    TO "engine_rw";
--> statement-breakpoint

-- Registries that anchor history. No DELETE: removing a competition or season
-- would orphan every fixture and prediction that references it.
GRANT SELECT, INSERT, UPDATE ON "competitions" TO "engine_rw";--> statement-breakpoint
GRANT SELECT, INSERT, UPDATE ON "seasons"      TO "engine_rw";
--> statement-breakpoint

-- Never delete a team - tombstone it via status (§6 rule 8). Enforced here by
-- withholding DELETE rather than by asking code to remember.
GRANT SELECT, INSERT, UPDATE ON "teams" TO "engine_rw";
--> statement-breakpoint

-- Temporal names are append-and-close. A row may be CLOSED but never EDITED:
-- rewriting `name` in place would silently relabel every historical page with
-- no error raised (§10.6).
GRANT SELECT, INSERT ON "team_names"        TO "engine_rw";--> statement-breakpoint
GRANT UPDATE ("valid_to") ON "team_names"   TO "engine_rw";--> statement-breakpoint
GRANT SELECT, INSERT ON "competition_names" TO "engine_rw";--> statement-breakpoint
GRANT UPDATE ("valid_to") ON "competition_names" TO "engine_rw";
--> statement-breakpoint

-- Aliases are matching metadata, not history: a wrong alias must be removable.
GRANT SELECT, INSERT, UPDATE, DELETE ON "team_aliases" TO "engine_rw";
--> statement-breakpoint

-- The web app reads the canonical layer and writes none of it.
GRANT SELECT ON "countries", "venues", "competitions", "competition_names",
                "seasons", "teams", "team_names", "team_aliases" TO "app_rw";
--> statement-breakpoint

GRANT SELECT ON "countries", "venues", "competitions", "competition_names",
                "seasons", "teams", "team_names", "team_aliases" TO "analytics_ro";
