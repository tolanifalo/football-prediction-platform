import {
  bigint,
  jsonb,
  pgTable,
  pgView,
  smallint,
  text,
  timestamp,
  uuid,
} from "drizzle-orm/pg-core";

/**
 * One row per observed fetch (PHASE-0-SPEC.md §9.1).
 *
 * PHYSICAL DDL IS OWNED BY A --custom SQL MIGRATION, because Drizzle cannot
 * express PARTITION BY. This declaration provides types and query support
 * only, and lives outside the drizzle.config.ts `schema` glob so that
 * `drizzle-kit generate` never tries to manage it (§8.1).
 *
 * Do not move this file under src/schema/ - generate would then emit a plain,
 * unpartitioned CREATE TABLE for it.
 *
 * Partitioned monthly by `fetched_at`. The primary key is (id, fetched_at):
 * a partitioned table's key must include its partition key, which is also why
 * nothing may declare a foreign key TO this table. Facts reference
 * raw_payload_bodies.id instead.
 *
 * Append-only: no UPDATE or DELETE is granted (§9.3).
 */
export const rawPayloads = pgTable("raw_payloads", {
  id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity(),
  sourceId: uuid("source_id").notNull(),
  jobRunId: bigint("job_run_id", { mode: "bigint" }).notNull(),
  /** ON DELETE RESTRICT: retention can never orphan cited evidence (§9.8). */
  bodyId: bigint("body_id", { mode: "bigint" }).notNull(),
  /** Endpoint template, e.g. "/v3/fixtures" - never the interpolated URL. */
  endpoint: text("endpoint").notNull(),
  /** sha256 over the canonical (endpoint + sorted params), credentials excluded. */
  requestSignature: text("request_signature").notNull(),
  /** REDACTED at the adapter boundary. Never store credentials here. */
  requestParams: jsonb("request_params").notNull().default({}),
  httpStatus: smallint("http_status"),
  /** Whitelisted at the adapter boundary: etag, last-modified, content-type, date. */
  responseHeaders: jsonb("response_headers").notNull().default({}),
  /** Partition key. Request completion, UTC. Anchors `known_at` on future facts. */
  fetchedAt: timestamp("fetched_at", { withTimezone: true }).notNull(),
});

/**
 * Derived seen-counts (§9.3). Replaces the mutable seen_count / last_seen_at
 * columns that would otherwise have sat on immutable evidence.
 *
 * `.existing()` declares the view for typing without Drizzle owning its DDL.
 */
export const vRawPayloadSeen = pgView("v_raw_payload_seen", {
  bodyId: bigint("body_id", { mode: "bigint" }).notNull(),
  seenCount: bigint("seen_count", { mode: "number" }).notNull(),
  firstSeenAt: timestamp("first_seen_at", { withTimezone: true }).notNull(),
  lastSeenAt: timestamp("last_seen_at", { withTimezone: true }).notNull(),
}).existing();
