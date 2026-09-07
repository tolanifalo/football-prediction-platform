import { bigserial, pgTable, text, timestamp } from "drizzle-orm/pg-core";

/**
 * P0-03 harness object whose physical DDL is owned by a --custom SQL
 * migration, because Drizzle cannot express PARTITION BY.
 *
 * This declaration provides types and query support only. It lives outside
 * the drizzle.config.ts `schema` glob, which is what keeps `drizzle-kit
 * generate` from trying to manage it (PHASE-0-SPEC.md 8.1).
 *
 * Do not move this file under src/schema/ - generate would then emit a plain
 * unpartitioned CREATE TABLE for it.
 */
export const harnessPartitioned = pgTable("harness_partitioned", {
  id: bigserial("id", { mode: "bigint" }),
  label: text("label").notNull(),
  observedAt: timestamp("observed_at", { withTimezone: true }).notNull(),
});
