import { sql } from "drizzle-orm";
import { bigint, check, customType, integer, pgTable, text, timestamp, unique } from "drizzle-orm/pg-core";

/** Drizzle has no built-in bytea; this is the minimal mapping. */
const bytea = customType<{ data: Buffer; driverData: Buffer }>({
  dataType: () => "bytea",
});

/**
 * One row per distinct response body - content-addressed evidence
 * (PHASE-0-SPEC.md §9.1, §9.4).
 *
 * Deliberately NOT partitioned. That is what allows the global uniqueness
 * below, and what lets a future fact reference this table with a single-column
 * foreign key. Both are impossible against a partitioned table.
 *
 * Representation rules, exactly as specified in §9.4 - each exists because the
 * opposite is a plausible mistake:
 *   - `body_hash` is SHA-256 of the response content AFTER transport decoding
 *     (after Content-Encoding: gzip is undone) and BEFORE any parsing.
 *   - `body`, when retained, holds exactly those same bytes. It is never
 *     labelled gzip merely because the HTTP response was gzipped; transport
 *     encoding is metadata about the response and belongs in
 *     raw_payloads.response_headers, not here.
 *   - JSON is never canonicalised before hashing. A canonicaliser's output can
 *     change with a library upgrade, silently invalidating every historical
 *     hash. Determinism across time beats deduplication efficiency.
 *   - `byte_size` is the length of the hashed content. Not the transfer size,
 *     not the stored size. There is no second size column.
 *
 * Append-only: no UPDATE or DELETE is granted (§9.3). Evidence is never
 * corrected - a provider correction is new bytes, so a new row.
 */
export const rawPayloadBodies = pgTable(
  "raw_payload_bodies",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    hashAlgo: text("hash_algo").notNull().default("sha256"),
    bodyHash: text("body_hash").notNull(),
    /** Nullable so future tiering can drop bytes while keeping provenance (§9.8). */
    body: bytea("body"),
    byteSize: integer("byte_size").notNull(),
    firstSeenAt: timestamp("first_seen_at", { withTimezone: true }).notNull().defaultNow(),
  },
  (t) => [
    // The global dedup constraint. Possible only because this table is not partitioned.
    unique("raw_payload_bodies_content_key").on(t.hashAlgo, t.bodyHash),
    check("raw_payload_bodies_hash_algo_check", sql`${t.hashAlgo} IN ('sha256')`),
    check("raw_payload_bodies_body_hash_check", sql`${t.bodyHash} ~ '^[0-9a-f]{64}$'`),
    check("raw_payload_bodies_byte_size_check", sql`${t.byteSize} >= 0`),
    // If the bytes are present they must be the bytes that were measured.
    check(
      "raw_payload_bodies_body_length_check",
      sql`${t.body} IS NULL OR octet_length(${t.body}) = ${t.byteSize}`,
    ),
  ],
);
