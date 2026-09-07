import { pgTable, serial, text, timestamp } from "drizzle-orm/pg-core";

/**
 * P0-03 harness table. Drizzle owns this DDL end to end.
 *
 * Exists only to prove migration generation and the empty-diff check. It is
 * NOT a domain table and carries no Phase 0 data. P0-04 introduces the first
 * real tables.
 */
export const harnessManaged = pgTable("harness_managed", {
  id: serial("id").primaryKey(),
  label: text("label").notNull(),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});
