import { boolean, integer, pgTable, text, timestamp, uuid } from "drizzle-orm/pg-core";

/**
 * Provider registry. Root of all provenance (PHASE-0-SPEC.md §1.3).
 *
 * Holds no credentials. API keys live only in the engine environment
 * (ARCHITECTURE.md §8); putting one here would leak it into every backup and
 * export of this database.
 *
 * UUID key per the registry/reference convention in §9.7.
 */
export const dataSources = pgTable("data_sources", {
  id: uuid("id").primaryKey().defaultRandom(),
  slug: text("slug").notNull().unique(),
  displayName: text("display_name").notNull(),
  /** Which feed kinds this source supplies: fixtures, results, stats, odds. */
  kinds: text("kinds").array().notNull().default([]),
  baseUrl: text("base_url"),
  rateLimitPerMin: integer("rate_limit_per_min"),
  isActive: boolean("is_active").notNull().default(true),
  createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
});
