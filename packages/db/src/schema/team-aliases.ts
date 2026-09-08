import { sql } from "drizzle-orm";
import {
  bigint, check, pgTable, smallint, text, timestamp, uniqueIndex, uuid,
} from "drizzle-orm/pg-core";

import { dataSources } from "./data-sources.ts";
import { teams } from "./teams.ts";

/**
 * Alias strings used to MATCH a team during ingestion (PHASE-0-SPEC.md §10.3).
 *
 * An alias is not display truth and not a historical name. "Man Utd" and
 * "Besiktas" are aliases; "Guangzhou Evergrande Taobao" is a team_names row.
 *
 * AMBIGUITY MUST FAIL, NEVER RESOLVE ARBITRARILY. "Barcelona" matches FC
 * Barcelona and Barcelona SC; "Arsenal" matches Arsenal FC, Arsenal Tula and
 * Arsenal Sarandi. The two partial unique indexes below make that a database
 * guarantee: a global alias can belong to exactly one team, and within one
 * provider an alias maps to exactly one team.
 *
 * source_id records WHICH PROVIDER contributed a spelling. This is not an
 * external-ID table: aliases hold human-readable strings, external_ids holds
 * opaque provider primary keys (P0-06).
 *
 * normalized_alias is normalised in APPLICATION CODE. unaccent() is not
 * immutable and cannot back a generated column without wrapping it; the
 * extension is avoided entirely.
 */
export const teamAliases = pgTable(
  "team_aliases",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    teamId: uuid("team_id").notNull().references(() => teams.id),
    alias: text("alias").notNull(),
    normalizedAlias: text("normalized_alias").notNull(),
    /** NULL = a global alias, valid regardless of provider. */
    sourceId: uuid("source_id").references(() => dataSources.id),
    confidence: smallint("confidence").notNull().default(100),
    createdAt: timestamp("created_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    // Two partial uniques rather than UNIQUE NULLS NOT DISTINCT: simpler, and
    // it does not depend on a PG15 feature whose Drizzle support is unverified.
    uniqueIndex("team_aliases_global_idx")
      .on(t.normalizedAlias)
      .where(sql`${t.sourceId} IS NULL`),
    uniqueIndex("team_aliases_source_idx")
      .on(t.sourceId, t.normalizedAlias)
      .where(sql`${t.sourceId} IS NOT NULL`),
    check("team_aliases_confidence_check", sql`${t.confidence} BETWEEN 0 AND 100`),
    check("team_aliases_normalized_lower_check", sql`${t.normalizedAlias} = lower(${t.normalizedAlias})`),
    check("team_aliases_normalized_nonempty_check", sql`length(${t.normalizedAlias}) > 0`),
  ],
);
