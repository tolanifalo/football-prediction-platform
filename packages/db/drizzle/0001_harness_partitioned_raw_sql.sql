-- Custom SQL migration file, put your code below! --

-- P0-03 harness: an object Drizzle cannot express (PARTITION BY), owned by
-- this migration rather than by `generate`. Its typed declaration lives in
-- src/raw-sql/harness-partitioned.ts, outside the config `schema` glob.
--
-- The SQL is inline here on purpose. Migrations are immutable once applied,
-- so this file is the only copy - there is no external .sql source that could
-- drift from the ledger (PHASE-0-SPEC.md 8.4).
--
-- A partitioned table's primary key must include the partition key.
CREATE TABLE "harness_partitioned" (
	"id" bigserial NOT NULL,
	"label" text NOT NULL,
	"observed_at" timestamptz NOT NULL,
	PRIMARY KEY ("id", "observed_at")
) PARTITION BY RANGE ("observed_at");
--> statement-breakpoint
-- A DEFAULT partition keeps the harness free of a date-bound expiry. Real
-- monthly partition management arrives with raw_payloads in P0-04.
CREATE TABLE "harness_partitioned_default" PARTITION OF "harness_partitioned" DEFAULT;
