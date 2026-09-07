/**
 * Live PostgreSQL invariant check for the P0-04 raw archive
 * (PHASE-0-SPEC.md §9.5).
 *
 * This is NOT db:verify-generate. That one never opens a connection and only
 * compares the Drizzle schema input against its snapshot. This one inspects
 * the actual database: partition structure, routing, referential protection
 * and the append-only grants. Nothing here reads a TypeScript declaration.
 *
 * Every mutating probe runs inside a transaction that is rolled back, so the
 * check leaves no evidence row behind and is safe to run repeatedly.
 */
import postgres from "postgres";

import { getDatabaseUrl } from "./connection.ts";

const PARENT = "raw_payloads";
const DEFAULT_PARTITION = "raw_payloads_default";
const ENGINE_ROLE = "engine_rw";

/** The 12 months pre-created by 0003. Kept in sync with that migration by hand. */
const EXPECTED_MONTHS = [
  "2026_09", "2026_10", "2026_11", "2026_12",
  "2027_01", "2027_02", "2027_03", "2027_04",
  "2027_05", "2027_06", "2027_07", "2027_08",
];

const EXPECTED_INDEXES = [
  "raw_payloads_signature_fetched_idx",
  "raw_payloads_body_id_idx",
  "raw_payloads_job_run_id_idx",
  "raw_payloads_source_fetched_idx",
];

const sql = postgres(getDatabaseUrl(), { max: 1, onnotice: () => {} });

let failures = 0;
const check = (name: string, ok: boolean, detail = ""): void => {
  if (ok) {
    console.log(`  PASS  ${name}`);
  } else {
    failures += 1;
    console.error(`  FAIL  ${name}${detail ? ` — ${detail}` : ""}`);
  }
};

/** Runs body in a transaction that always rolls back. */
const inRollback = async <T>(fn: (tx: postgres.TransactionSql) => Promise<T>): Promise<T> => {
  const sentinel = Symbol("rollback");
  let captured: T;
  try {
    await sql.begin(async (tx) => {
      captured = await fn(tx);
      throw sentinel;
    });
  } catch (error: unknown) {
    if (error !== sentinel) throw error;
  }
  return captured!;
};

/** True when running `stmt` as engine_rw raises insufficient_privilege (42501). */
const isRefusedForEngine = async (stmt: string): Promise<{ refused: boolean; code: string }> => {
  let code = "none";
  await inRollback(async (tx) => {
    try {
      await tx.unsafe(`SET LOCAL ROLE ${ENGINE_ROLE}`);
      await tx.unsafe(stmt);
    } catch (error: unknown) {
      code = (error as { code?: string }).code ?? "unknown";
    }
  });
  return { refused: code === "42501", code };
};

try {
  console.log("raw_payloads partition and evidence invariants\n");

  // 1. parent is partitioned
  const [kind] = await sql<{ relkind: string }[]>`
    SELECT c.relkind::text FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = ${PARENT}`;
  check("1. parent relkind = 'p'", kind?.relkind === "p", `got ${kind?.relkind ?? "missing"}`);

  // 2. partition key is exactly fetched_at
  const [key] = await sql<{ def: string }[]>`
    SELECT pg_get_partkeydef(c.oid) AS def FROM pg_class c
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE n.nspname = 'public' AND c.relname = ${PARENT}`;
  check("2. partition key is RANGE (fetched_at)", key?.def === "RANGE (fetched_at)", key?.def);

  // 3. primary key includes the partition key
  const [pk] = await sql<{ cols: string }[]>`
    SELECT string_agg(a.attname, ',' ORDER BY k.ord) AS cols
      FROM pg_constraint con
      JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
      JOIN pg_attribute a ON a.attrelid = con.conrelid AND a.attnum = k.attnum
     WHERE con.conrelid = ${PARENT}::regclass AND con.contype = 'p'`;
  check("3. primary key includes fetched_at", (pk?.cols ?? "").split(",").includes("fetched_at"), pk?.cols);

  // 4/5. expected bounds present, contiguous, non-overlapping
  const parts = await sql<{ relname: string; bounds: string }[]>`
    SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) AS bounds
      FROM pg_class c
      JOIN pg_inherits i ON i.inhrelid = c.oid
     WHERE i.inhparent = ${PARENT}::regclass
     ORDER BY c.relname`;

  const ranges = parts
    .filter((p) => p.bounds !== "DEFAULT")
    .map((p) => {
      const m = /FROM \('([^']+)'\) TO \('([^']+)'\)/.exec(p.bounds);
      return { name: p.relname, from: m?.[1] ?? "", to: m?.[2] ?? "" };
    })
    .sort((a, b) => a.from.localeCompare(b.from));

  const missing = EXPECTED_MONTHS.filter(
    (m) => !ranges.some((r) => r.name === `raw_payloads_${m}`),
  );
  check("4. all 12 expected monthly partitions present", missing.length === 0, `missing ${missing.join(", ")}`);

  const gaps = ranges
    .slice(0, -1)
    .map((r, i) => (r.to === ranges[i + 1]?.from ? null : `${r.name} ends ${r.to}, next starts ${ranges[i + 1]?.from}`))
    .filter((g): g is string => g !== null);
  check("5. bounds contiguous and non-overlapping", gaps.length === 0, gaps.join("; "));

  // 6. DEFAULT exists and is empty
  const hasDefault = parts.some((p) => p.relname === DEFAULT_PARTITION && p.bounds === "DEFAULT");
  check("6a. DEFAULT partition exists", hasDefault);
  const [def] = await sql<{ n: number }[]>`SELECT count(*)::int AS n FROM ONLY raw_payloads_default`;
  check(
    "6b. DEFAULT partition is EMPTY",
    def?.n === 0,
    `${def?.n} row(s) — a month is missing and can no longer be attached (§9.5)`,
  );

  // 7. routing probe, rolled back
  const month = EXPECTED_MONTHS[0]!.replace("_", "-");
  const probeAt = `${month}-15T12:00:00Z`;
  const landed = await inRollback(async (tx) => {
    const [src] = await tx<{ id: string }[]>`
      INSERT INTO data_sources (slug, display_name) VALUES ('probe', 'probe') RETURNING id`;
    const [run] = await tx<{ id: string }[]>`
      INSERT INTO job_runs (job_name, run_date, status) VALUES ('probe', CURRENT_DATE, 'running') RETURNING id`;
    const [body] = await tx<{ id: string }[]>`
      INSERT INTO raw_payload_bodies (body_hash, byte_size) VALUES (repeat('a', 64), 0) RETURNING id`;
    const [row] = await tx<{ partition: string }[]>`
      INSERT INTO raw_payloads (source_id, job_run_id, body_id, endpoint, request_signature, fetched_at)
      VALUES (${src!.id}, ${run!.id}, ${body!.id}, '/probe', 'probe', ${probeAt})
      RETURNING tableoid::regclass::text AS partition`;
    return row!.partition;
  });
  check(
    `7. routing probe (${probeAt}) reaches its month's partition`,
    landed === `raw_payloads_${EXPECTED_MONTHS[0]}`,
    `landed in ${landed}`,
  );

  // 8. partition count not below expectation
  check("8. partition count >= 13 (12 months + DEFAULT)", parts.length >= 13, `found ${parts.length}`);

  // 9. expected indexes on parent, inherited by every partition
  const idx = await sql<{ indexname: string }[]>`
    SELECT indexname FROM pg_indexes WHERE schemaname = 'public' AND tablename = ${PARENT}`;
  const parentIdx = idx.map((i) => i.indexname);
  const missingIdx = EXPECTED_INDEXES.filter((e) => !parentIdx.includes(e));
  check("9a. expected indexes exist on parent", missingIdx.length === 0, `missing ${missingIdx.join(", ")}`);
  const [inherited] = await sql<{ bad: number }[]>`
    SELECT count(*)::int AS bad FROM pg_inherits i
     WHERE i.inhparent = ${PARENT}::regclass
       AND (SELECT count(*) FROM pg_index x WHERE x.indrelid = i.inhrelid) <
           (SELECT count(*) FROM pg_index x WHERE x.indrelid = ${PARENT}::regclass)`;
  check("9b. every partition inherited the parent's indexes", inherited?.bad === 0, `${inherited?.bad} lagging`);

  // 10. every observation resolves to a body
  const [orphans] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM raw_payloads p
     WHERE NOT EXISTS (SELECT 1 FROM raw_payload_bodies b WHERE b.id = p.body_id)`;
  check("10. all raw_payloads rows resolve to a body", orphans?.n === 0, `${orphans?.n} orphan(s)`);

  // 11. ON DELETE RESTRICT protects referenced bodies (tested as owner, so the
  //     grant layer cannot mask the FK behaviour)
  const restrict = await inRollback(async (tx) => {
    const [src] = await tx<{ id: string }[]>`
      INSERT INTO data_sources (slug, display_name) VALUES ('probe2', 'probe2') RETURNING id`;
    const [run] = await tx<{ id: string }[]>`
      INSERT INTO job_runs (job_name, run_date, status) VALUES ('probe2', CURRENT_DATE, 'running') RETURNING id`;
    const [body] = await tx<{ id: string }[]>`
      INSERT INTO raw_payload_bodies (body_hash, byte_size) VALUES (repeat('b', 64), 0) RETURNING id`;
    await tx`INSERT INTO raw_payloads (source_id, job_run_id, body_id, endpoint, request_signature, fetched_at)
             VALUES (${src!.id}, ${run!.id}, ${body!.id}, '/probe', 'probe', ${probeAt})`;
    try {
      await tx`DELETE FROM raw_payload_bodies WHERE id = ${body!.id}`;
      return "not-blocked";
    } catch (error: unknown) {
      return (error as { code?: string }).code ?? "unknown";
    }
  });
  check("11. ON DELETE RESTRICT blocks deleting a referenced body", restrict === "23503", `code ${restrict}`);

  // 12. duplicate content yields one body row and many observations
  const dedup = await inRollback(async (tx) => {
    const [src] = await tx<{ id: string }[]>`
      INSERT INTO data_sources (slug, display_name) VALUES ('probe3', 'probe3') RETURNING id`;
    const [run] = await tx<{ id: string }[]>`
      INSERT INTO job_runs (job_name, run_date, status) VALUES ('probe3', CURRENT_DATE, 'running') RETURNING id`;
    const hash = repeatHash("c");
    for (let i = 0; i < 2; i += 1) {
      await tx`INSERT INTO raw_payload_bodies (body_hash, byte_size) VALUES (${hash}, 0)
               ON CONFLICT (hash_algo, body_hash) DO NOTHING`;
      const [body] = await tx<{ id: string }[]>`
        SELECT id FROM raw_payload_bodies WHERE hash_algo = 'sha256' AND body_hash = ${hash}`;
      await tx`INSERT INTO raw_payloads (source_id, job_run_id, body_id, endpoint, request_signature, fetched_at)
               VALUES (${src!.id}, ${run!.id}, ${body!.id}, '/probe', 'probe', ${probeAt})`;
    }
    const [bodies] = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM raw_payload_bodies WHERE body_hash = ${hash}`;
    const [seen] = await tx<{ seen_count: number }[]>`
      SELECT v.seen_count::int FROM v_raw_payload_seen v
        JOIN raw_payload_bodies b ON b.id = v.body_id WHERE b.body_hash = ${hash}`;
    return { bodies: bodies?.n, seen: seen?.seen_count };
  });
  check(
    "12. duplicate content → 1 body row, 2 observations, v_raw_payload_seen.seen_count = 2",
    dedup.bodies === 1 && dedup.seen === 2,
    `bodies=${dedup.bodies} seen_count=${dedup.seen}`,
  );

  // 13. append-only enforced by grant, not convention — negative tests
  console.log("\n  append-only grants (engine_rw must be refused):");
  for (const [name, stmt] of [
    ["UPDATE raw_payload_bodies", "UPDATE raw_payload_bodies SET byte_size = 1"],
    ["DELETE raw_payload_bodies", "DELETE FROM raw_payload_bodies"],
    ["UPDATE raw_payloads", "UPDATE raw_payloads SET endpoint = 'x'"],
    ["DELETE raw_payloads", "DELETE FROM raw_payloads"],
    ["UPDATE job_runs.job_name", "UPDATE job_runs SET job_name = 'x'"],
    ["UPDATE job_runs.started_at", "UPDATE job_runs SET started_at = now()"],
    ["DELETE job_runs", "DELETE FROM job_runs"],
  ] as const) {
    const { refused, code } = await isRefusedForEngine(stmt);
    check(`13. refused: ${name}`, refused, `expected 42501, got ${code}`);
  }

  console.log("\n  permitted job_runs updates (engine_rw must succeed):");
  const allowed = await inRollback(async (tx) => {
    await tx`INSERT INTO job_runs (job_name, run_date, status) VALUES ('probe4', CURRENT_DATE, 'running')`;
    try {
      await tx.unsafe(`SET LOCAL ROLE ${ENGINE_ROLE}`);
      await tx`UPDATE job_runs SET status = 'ok', finished_at = now(), stats = '{"n":1}'::jsonb, error = NULL
                WHERE job_name = 'probe4'`;
      return "ok";
    } catch (error: unknown) {
      return (error as { code?: string }).code ?? "unknown";
    }
  });
  check("14. permitted: UPDATE job_runs (status, finished_at, stats, error)", allowed === "ok", allowed);

  console.log(failures === 0 ? "\nall invariants hold" : `\n${failures} invariant(s) violated`);
  if (failures > 0) process.exitCode = 1;
} finally {
  await sql.end({ timeout: 5 });
}

function repeatHash(ch: string): string {
  return ch.repeat(64);
}
