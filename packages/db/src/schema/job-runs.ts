import { sql } from "drizzle-orm";
import {
  bigint,
  check,
  date,
  index,
  integer,
  jsonb,
  pgTable,
  text,
  timestamp,
  unique,
  uuid,
} from "drizzle-orm/pg-core";

import { dataSources } from "./data-sources.ts";

/**
 * One row per invocation of one job against one source (PHASE-0-SPEC.md §9.2).
 *
 * Absorbs what was once planned as `ingestion_runs`: `source_id` and
 * `adapter_version` are set for ingest jobs and null for everything else.
 * A provider *request* is not a run - it is a `raw_payloads` observation.
 *
 * The only table in the provenance set that accepts UPDATE, and only on
 * (status, finished_at, stats, error) so a run can be closed (§9.3).
 */
export const jobRuns = pgTable(
  "job_runs",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    jobName: text("job_name").notNull(),
    /** Narrows the run, e.g. "championship:2023-24". Empty string when global. */
    scopeKey: text("scope_key").notNull().default(""),
    runDate: date("run_date").notNull(),
    attempt: integer("attempt").notNull().default(1),
    sourceId: uuid("source_id").references(() => dataSources.id),
    adapterVersion: text("adapter_version"),
    params: jsonb("params").notNull().default({}),
    status: text("status").notNull(),
    startedAt: timestamp("started_at", { withTimezone: true }).notNull().defaultNow(),
    finishedAt: timestamp("finished_at", { withTimezone: true }),
    stats: jsonb("stats").notNull().default({}),
    error: text("error"),
  },
  (t) => [
    // Idempotency key: re-running the same job for the same scope and date is
    // a new attempt, never a silent duplicate.
    unique("job_runs_identity_key").on(t.jobName, t.scopeKey, t.runDate, t.attempt),
    check("job_runs_status_check", sql`${t.status} IN ('running', 'ok', 'partial', 'failed')`),
    check("job_runs_attempt_check", sql`${t.attempt} >= 1`),
    check(
      "job_runs_finished_after_started_check",
      sql`${t.finishedAt} IS NULL OR ${t.finishedAt} >= ${t.startedAt}`,
    ),
    index("job_runs_job_name_run_date_idx").on(t.jobName, t.runDate.desc()),
    index("job_runs_running_idx").on(t.status).where(sql`${t.status} = 'running'`),
  ],
);
