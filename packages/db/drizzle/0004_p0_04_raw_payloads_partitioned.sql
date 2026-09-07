-- Custom SQL migration file, put your code below! --

-- P0-04: raw_payloads, the observation half of the raw archive
-- (PHASE-0-SPEC.md §9.1). Drizzle cannot express PARTITION BY, so this DDL is
-- owned here. Typed declaration: src/raw-sql/raw-payloads.ts.
--
-- SQL is inline on purpose: migrations are immutable once applied, so this
-- file is the only copy and nothing can drift from it (§8.4).

CREATE TABLE "raw_payloads" (
	"id"                bigint GENERATED ALWAYS AS IDENTITY,
	"source_id"         uuid        NOT NULL REFERENCES "data_sources" ("id"),
	"job_run_id"        bigint      NOT NULL REFERENCES "job_runs" ("id"),
	-- RESTRICT so retention can never orphan evidence a fact cites (§9.8).
	"body_id"           bigint      NOT NULL REFERENCES "raw_payload_bodies" ("id") ON DELETE RESTRICT,
	"endpoint"          text        NOT NULL,
	"request_signature" text        NOT NULL,
	"request_params"    jsonb       NOT NULL DEFAULT '{}'::jsonb,
	"http_status"       smallint,
	"response_headers"  jsonb       NOT NULL DEFAULT '{}'::jsonb,
	"fetched_at"        timestamptz NOT NULL,
	-- A partitioned table's key must include the partition key. This is also
	-- why nothing may declare a foreign key TO this table.
	PRIMARY KEY ("id", "fetched_at"),
	CONSTRAINT "raw_payloads_http_status_check"
		CHECK ("http_status" IS NULL OR ("http_status" BETWEEN 100 AND 599)),
	-- Defence in depth for the redaction rule. The adapter boundary is the real
	-- guard; this catches top-level credential keys reaching the archive.
	CONSTRAINT "raw_payloads_request_params_redacted_check"
		CHECK (NOT jsonb_exists_any("request_params",
			ARRAY['api_key','apikey','key','token','secret','password','auth','authorization'])),
	CONSTRAINT "raw_payloads_response_headers_whitelisted_check"
		CHECK (NOT jsonb_exists_any("response_headers",
			ARRAY['set-cookie','cookie','authorization','x-api-key']))
) PARTITION BY RANGE ("fetched_at");
--> statement-breakpoint

-- Twelve months pre-created, UTC bounds (§9.6). Phase 1 owns automatic
-- creation of month 13; Phase 0 has no scheduler.
CREATE TABLE "raw_payloads_2026_09" PARTITION OF "raw_payloads" FOR VALUES FROM ('2026-09-01Z') TO ('2026-10-01Z');--> statement-breakpoint
CREATE TABLE "raw_payloads_2026_10" PARTITION OF "raw_payloads" FOR VALUES FROM ('2026-10-01Z') TO ('2026-11-01Z');--> statement-breakpoint
CREATE TABLE "raw_payloads_2026_11" PARTITION OF "raw_payloads" FOR VALUES FROM ('2026-11-01Z') TO ('2026-12-01Z');--> statement-breakpoint
CREATE TABLE "raw_payloads_2026_12" PARTITION OF "raw_payloads" FOR VALUES FROM ('2026-12-01Z') TO ('2027-01-01Z');--> statement-breakpoint
CREATE TABLE "raw_payloads_2027_01" PARTITION OF "raw_payloads" FOR VALUES FROM ('2027-01-01Z') TO ('2027-02-01Z');--> statement-breakpoint
CREATE TABLE "raw_payloads_2027_02" PARTITION OF "raw_payloads" FOR VALUES FROM ('2027-02-01Z') TO ('2027-03-01Z');--> statement-breakpoint
CREATE TABLE "raw_payloads_2027_03" PARTITION OF "raw_payloads" FOR VALUES FROM ('2027-03-01Z') TO ('2027-04-01Z');--> statement-breakpoint
CREATE TABLE "raw_payloads_2027_04" PARTITION OF "raw_payloads" FOR VALUES FROM ('2027-04-01Z') TO ('2027-05-01Z');--> statement-breakpoint
CREATE TABLE "raw_payloads_2027_05" PARTITION OF "raw_payloads" FOR VALUES FROM ('2027-05-01Z') TO ('2027-06-01Z');--> statement-breakpoint
CREATE TABLE "raw_payloads_2027_06" PARTITION OF "raw_payloads" FOR VALUES FROM ('2027-06-01Z') TO ('2027-07-01Z');--> statement-breakpoint
CREATE TABLE "raw_payloads_2027_07" PARTITION OF "raw_payloads" FOR VALUES FROM ('2027-07-01Z') TO ('2027-08-01Z');--> statement-breakpoint
CREATE TABLE "raw_payloads_2027_08" PARTITION OF "raw_payloads" FOR VALUES FROM ('2027-08-01Z') TO ('2027-09-01Z');--> statement-breakpoint

-- Data-safety net. Must stay EMPTY: a row here means a month is missing, and
-- once one lands the missing month can no longer be attached (§9.5).
-- db:verify-partitions is the alarm.
CREATE TABLE "raw_payloads_default" PARTITION OF "raw_payloads" DEFAULT;
--> statement-breakpoint

-- Plain CREATE INDEX on the parent cascades to every partition. CONCURRENTLY
-- is unavailable on partitioned tables and inside the migrator's transaction
-- (§9.7); harmless here because the table is empty.
CREATE INDEX "raw_payloads_signature_fetched_idx" ON "raw_payloads" ("request_signature", "fetched_at" DESC);--> statement-breakpoint
CREATE INDEX "raw_payloads_body_id_idx"           ON "raw_payloads" ("body_id");--> statement-breakpoint
CREATE INDEX "raw_payloads_job_run_id_idx"        ON "raw_payloads" ("job_run_id");--> statement-breakpoint
CREATE INDEX "raw_payloads_source_fetched_idx"    ON "raw_payloads" ("source_id", "fetched_at" DESC);
--> statement-breakpoint

-- Seen-counts derived from observations, never stored as mutable state (§9.3).
CREATE VIEW "v_raw_payload_seen" AS
SELECT "body_id",
       count(*)          AS "seen_count",
       min("fetched_at") AS "first_seen_at",
       max("fetched_at") AS "last_seen_at"
  FROM "raw_payloads"
 GROUP BY "body_id";
--> statement-breakpoint
-- P0-04: append-only enforced by the database, not by convention
-- (PHASE-0-SPEC.md §9.3).
--
-- Raw evidence is immutable: engine_rw may SELECT and INSERT, and is granted
-- no UPDATE and no DELETE at all. job_runs is the sole exception, receiving
-- UPDATE on exactly four columns so a run can be closed.
--
-- NOTE: this creates the engine_rw role. P0-16 completes the role model
-- (app_rw, analytics_ro, RLS); only what P0-04's own invariants require is
-- created here.

DO $$
BEGIN
	IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'engine_rw') THEN
		CREATE ROLE "engine_rw" NOLOGIN;
	END IF;
END
$$;
--> statement-breakpoint

GRANT USAGE ON SCHEMA "public" TO "engine_rw";
--> statement-breakpoint

-- Registry: readable, writable, correctable.
GRANT SELECT, INSERT, UPDATE, DELETE ON "data_sources" TO "engine_rw";
--> statement-breakpoint

-- Run ledger: a run is opened, then closed. Its identity and start time are as
-- immutable as the evidence it collected.
GRANT SELECT, INSERT ON "job_runs" TO "engine_rw";--> statement-breakpoint
GRANT UPDATE ("status", "finished_at", "stats", "error") ON "job_runs" TO "engine_rw";
--> statement-breakpoint

-- Evidence: insert and read only. No UPDATE, no DELETE, ever.
GRANT SELECT, INSERT ON "raw_payload_bodies" TO "engine_rw";--> statement-breakpoint
GRANT SELECT, INSERT ON "raw_payloads" TO "engine_rw";--> statement-breakpoint
GRANT SELECT ON "v_raw_payload_seen" TO "engine_rw";
