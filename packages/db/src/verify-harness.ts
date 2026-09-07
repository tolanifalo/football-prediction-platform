/**
 * Proves the P0-03 harness end to end against a live database:
 *   - the Drizzle-managed table exists and is queryable;
 *   - the raw-SQL-owned object exists and is genuinely partitioned;
 *   - its declaration under src/raw-sql/ is typed and queryable even though
 *     `generate` never sees it.
 *
 * Requires a running database. Not part of `pnpm verify` for that reason.
 */
import { sql as raw } from "drizzle-orm";
import { drizzle } from "drizzle-orm/postgres-js";
import postgres from "postgres";

import { getDatabaseUrl } from "./connection.ts";
import { harnessPartitioned } from "./raw-sql/harness-partitioned.ts";
import { harnessManaged } from "./schema/harness.ts";

const client = postgres(getDatabaseUrl(), { max: 1, onnotice: () => {} });
const db = drizzle(client);

try {
  // Drizzle-managed table: insert and read back.
  await db.insert(harnessManaged).values({ label: "p0-03" });
  const managed = await db.select().from(harnessManaged).orderBy(harnessManaged.id);
  console.log(`harness_managed rows: ${managed.length}, last label: ${managed.at(-1)?.label}`);

  // Raw-SQL-owned object: typed insert/select through the declaration that
  // generate cannot see.
  const observedAt = new Date("2026-09-07T12:00:00Z");
  // No explicit id: bigserial assigns it, so the script is re-runnable against
  // a database that already holds harness rows.
  await db.insert(harnessPartitioned).values({ label: "p0-03-raw", observedAt });
  const rows = await db
    .select()
    .from(harnessPartitioned)
    .orderBy(harnessPartitioned.id);
  const row = rows.at(-1);
  console.log(`harness_partitioned rows: ${rows.length}, last label: ${row?.label}`);

  // The row must have landed in a partition, not the parent.
  const placement = await db.execute<{ partition: string }>(
    raw`SELECT tableoid::regclass::text AS partition FROM harness_partitioned ORDER BY id`,
  );
  console.log(`row physically stored in: ${placement.at(-1)?.partition}`);

  // The parent must still be a partitioned table.
  const kind = await db.execute<{ relkind: string; partitions: number }>(
    raw`SELECT c.relkind::text AS relkind,
               (SELECT count(*) FROM pg_inherits WHERE inhparent = c.oid)::int AS partitions
          FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname = 'public' AND c.relname = 'harness_partitioned'`,
  );
  console.log(`harness_partitioned relkind=${kind[0]?.relkind} partitions=${kind[0]?.partitions}`);

  if (kind[0]?.relkind !== "p") {
    throw new Error("harness_partitioned is not a partitioned table");
  }
  console.log("harness verified");
} finally {
  await client.end({ timeout: 5 });
}
