import { sql } from "drizzle-orm";
import {
  bigint, check, index, pgTable, smallint, text, timestamp, uniqueIndex, uuid,
} from "drizzle-orm/pg-core";

import { dataSources } from "./data-sources.ts";

/**
 * Provider primary key -> canonical UUID (PHASE-0-SPEC.md §11).
 *
 * The first FULL BITEMPORAL table (§2.1): `known_at` is when we learned a
 * mapping, `superseded_at` when we learned a different one. Read it only
 * through the as-of mechanism (`external_ids_as_of`), never by hand-writing
 * the visibility predicate (§11.1).
 *
 * `internal_id` is POLYMORPHIC: `entity_type` selects which canonical registry
 * it points at. A single column cannot carry a foreign key to five tables, so
 * integrity is trigger-enforced (§11.3) - it is NOT a PostgreSQL FK and
 * information_schema will not report it as one.
 *
 * A remap NEVER rewrites `internal_id`. It supersedes the old row and inserts
 * a new one; the triggers make the alternative impossible (§6 rule 9,
 * "Never auto-remap").
 */
export const externalIds = pgTable(
  "external_ids",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    sourceId: uuid("source_id").notNull().references(() => dataSources.id),
    entityType: text("entity_type").notNull(),
    externalId: text("external_id").notNull(),
    /** Polymorphic: resolved against the registry named by entity_type. */
    internalId: uuid("internal_id").notNull(),
    /** Stored here; its MEANING belongs to P0-12 (§11.5). No threshold is defined. */
    confidence: smallint("confidence"),
    lastVerifiedAt: timestamp("last_verified_at", { withTimezone: true }),
    knownAt: timestamp("known_at", { withTimezone: true }).notNull().defaultNow(),
    supersededAt: timestamp("superseded_at", { withTimezone: true }),
  },
  (t) => [
    // Many external IDs may map to one internal UUID. One CURRENT external ID
    // may not map to two (§6 rule 10).
    uniqueIndex("external_ids_current_idx")
      .on(t.sourceId, t.entityType, t.externalId)
      .where(sql`${t.supersededAt} IS NULL`),
    // Supports the canonical-side delete trigger's reference lookup.
    index("external_ids_internal_idx").on(t.entityType, t.internalId),
    index("external_ids_known_at_idx").on(t.knownAt),
    check(
      "external_ids_entity_type_check",
      sql`${t.entityType} IN ('country','competition','season','team','venue')`,
    ),
    check(
      "external_ids_superseded_after_known_check",
      sql`${t.supersededAt} IS NULL OR ${t.supersededAt} > ${t.knownAt}`,
    ),
    check(
      "external_ids_confidence_check",
      sql`${t.confidence} IS NULL OR ${t.confidence} BETWEEN 0 AND 100`,
    ),
  ],
);
