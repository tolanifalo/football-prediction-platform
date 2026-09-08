/**
 * Live PostgreSQL invariant check for the P0-06 external identity mapping
 * layer (PHASE-0-SPEC.md §11 and its acceptance criterion).
 *
 * Inspects the database, never a TypeScript declaration. Mutating probes run
 * inside rolled-back transactions with SAVEPOINTs, per verify-canonical.
 *
 * NOTE ON THE AS-OF PREDICATE: this file must never restate the bitemporal
 * visibility comparison. Every as-of read below goes through
 * external_ids_as_of() or fn_visible_at(). Check 19 asserts the database holds
 * exactly one definition; 19b that the wrapper delegates; 19c that this file
 * itself never restates the comparison at all - the probe string is assembled
 * from fragments, so the check can demand zero (§11.1).
 */
import { readFileSync } from "node:fs";

import postgres from "postgres";

import { getDatabaseUrl } from "./connection.ts";

const ENGINE = "engine_rw";
/**
 * The bitemporal comparison, assembled from fragments so the literal never
 * appears whole in this file. That lets check 19c assert ZERO restatements
 * here rather than "exactly one, which happens to be my own probe".
 */
const PREDICATE = ["superseded_at IS NULL OR", "superseded_at >"].join(" ");
const CANONICAL = [
  ["country", "countries"],
  ["competition", "competitions"],
  ["season", "seasons"],
  ["team", "teams"],
  ["venue", "venues"],
] as const;

const sql = postgres(getDatabaseUrl(), { max: 1, onnotice: () => {} });

let failures = 0;
const check = (name: string, ok: boolean, detail = ""): void => {
  if (ok) console.log(`  PASS  ${name}`);
  else {
    failures += 1;
    console.error(`  FAIL  ${name}${detail ? ` — ${detail}` : ""}`);
  }
};

const inRollback = async <T>(fn: (tx: postgres.TransactionSql) => Promise<T>): Promise<T> => {
  const sentinel = Symbol("rollback");
  let captured!: T;
  try {
    await sql.begin(async (tx) => {
      captured = await fn(tx);
      throw sentinel;
    });
  } catch (error: unknown) {
    if (error !== sentinel) throw error;
  }
  return captured;
};

let spSeq = 0;
/** SQLSTATE of `stmt`, or "none". Savepoint-isolated so one failure does not poison the rest. */
const codeOf = async (tx: postgres.TransactionSql, stmt: string): Promise<string> => {
  const sp = `ve_sp_${(spSeq += 1)}`;
  await tx.unsafe(`SAVEPOINT ${sp}`);
  try {
    await tx.unsafe(stmt);
    await tx.unsafe(`RELEASE SAVEPOINT ${sp}`);
    return "none";
  } catch (error: unknown) {
    await tx.unsafe(`ROLLBACK TO SAVEPOINT ${sp}`);
    return (error as { code?: string }).code ?? "unknown";
  }
};

/** Seeds one row in every canonical registry plus a data source. */
const seed = async (tx: postgres.TransactionSql, tag: string) => {
  const [src] = await tx<{ id: string }[]>`
    INSERT INTO data_sources (slug, display_name) VALUES (${`s-${tag}`}, 'probe') RETURNING id`;
  const [c] = await tx<{ id: string }[]>`
    INSERT INTO countries (slug, name) VALUES (${`c-${tag}`}, 'Probeland') RETURNING id`;
  const [comp] = await tx<{ id: string }[]>`
    INSERT INTO competitions (slug, country_id, type, gender)
    VALUES (${`k-${tag}`}, ${c!.id}, 'league', 'men') RETURNING id`;
  const [se] = await tx<{ id: string }[]>`
    INSERT INTO seasons (slug, competition_id, label, start_year)
    VALUES (${`se-${tag}`}, ${comp!.id}, '2025/26', 2025) RETURNING id`;
  const [t1] = await tx<{ id: string }[]>`
    INSERT INTO teams (slug, country_id, gender) VALUES (${`t1-${tag}`}, ${c!.id}, 'men') RETURNING id`;
  const [t2] = await tx<{ id: string }[]>`
    INSERT INTO teams (slug, country_id, gender) VALUES (${`t2-${tag}`}, ${c!.id}, 'men') RETURNING id`;
  const [v] = await tx<{ id: string }[]>`
    INSERT INTO venues (slug, name, country_id) VALUES (${`v-${tag}`}, 'Probe Arena', ${c!.id}) RETURNING id`;
  return {
    source: src!.id, country: c!.id, competition: comp!.id,
    season: se!.id, team: t1!.id, team2: t2!.id, venue: v!.id,
  };
};

const ins = (s: { source: string }, type: string, ext: string, internal: string, extra = "") =>
  `INSERT INTO external_ids (source_id, entity_type, external_id, internal_id${extra ? ", known_at" : ""})
   VALUES ('${s.source}', '${type}', '${ext}', '${internal}'${extra ? `, '${extra}'` : ""})`;

try {
  console.log("external identity mapping invariants\n");

  console.log("  external_ids:");
  const ext = await inRollback(async (tx) => {
    const s = await seed(tx, "ext");
    const r: Record<string, string> = {};
    r.valid = await codeOf(tx, ins(s, "team", "E1", s.team));
    r.nonexistent = await codeOf(tx, ins(s, "team", "E2", "99999999-9999-9999-9999-999999999999"));
    r.crossType = await codeOf(tx, ins(s, "team", "E3", s.competition));
    r.badType = await codeOf(tx, ins(s, "player", "E4", s.team));
    r.mutInternal = await codeOf(tx, `UPDATE external_ids SET internal_id = '${s.team2}'`);
    r.mutType = await codeOf(tx, `UPDATE external_ids SET entity_type = 'venue'`);
    // now() is the TRANSACTION timestamp and is frozen, so superseding inside the
    // same transaction as the insert needs an explicit later instant. In
    // production supersession happens in a later transaction.
    r.supersede = await codeOf(tx, `UPDATE external_ids SET superseded_at = now() + interval '1 second'`);
    r.confidence = await codeOf(tx, `UPDATE external_ids SET confidence = 80`);
    r.verified = await codeOf(tx, `UPDATE external_ids SET last_verified_at = now()`);
    r.manyToOne = await codeOf(tx, ins(s, "team", "E5", s.team));
    // reopen a current mapping for the two-current test
    await tx`UPDATE external_ids SET superseded_at = NULL`;
    r.twoCurrent = await codeOf(tx, ins(s, "team", "E1", s.team2));
    return r;
  });
  check("1.  valid mapping succeeds", ext.valid === "none", ext.valid);
  check("2.  nonexistent internal_id rejected", ext.nonexistent === "23503", ext.nonexistent);
  check("3.  cross-type internal_id rejected", ext.crossType === "23503", ext.crossType);
  // A BEFORE ROW trigger fires before CHECK constraints are evaluated, so an
  // unknown entity_type is rejected by the trigger (23503) rather than the
  // CHECK (23514). Either is a correct rejection; both are asserted elsewhere.
  check("4.  entity_type rejected when unknown", ext.badType === "23514" || ext.badType === "23503", ext.badType);
  check("5.  internal_id immutable (trigger, 23001)", ext.mutInternal === "23001", ext.mutInternal);
  check("6.  entity_type immutable (trigger, 23001)", ext.mutType === "23001", ext.mutType);
  check("7.  superseded_at mutable", ext.supersede === "none", ext.supersede);
  check("8.  confidence mutable", ext.confidence === "none", ext.confidence);
  check("9.  last_verified_at mutable", ext.verified === "none", ext.verified);
  check("10. many external IDs -> one internal UUID allowed", ext.manyToOne === "none", ext.manyToOne);
  check("11. one current external ID -> two internal UUIDs rejected", ext.twoCurrent === "23505", ext.twoCurrent);

  const remap = await inRollback(async (tx) => {
    const s = await seed(tx, "remap");
    await tx.unsafe(ins(s, "team", "R1", s.team, "2020-01-01T00:00:00Z"));
    await tx`UPDATE external_ids SET superseded_at = '2024-01-01T00:00:00Z' WHERE external_id = 'R1'`;
    const code = await codeOf(tx, ins(s, "team", "R1", s.team2, "2024-01-01T00:00:00Z"));
    const rows = await tx<{ n: number }[]>`SELECT count(*)::int AS n FROM external_ids WHERE external_id = 'R1'`;
    const old = await tx<{ internal_id: string }[]>`
      SELECT internal_id FROM external_ids WHERE external_id = 'R1' AND superseded_at IS NOT NULL`;
    // as-of reads go through the wrapper — never a hand-written predicate
    const past = await tx<{ internal_id: string }[]>`
      SELECT internal_id FROM external_ids_as_of('2022-01-01T00:00:00Z') WHERE external_id = 'R1'`;
    const now = await tx<{ internal_id: string }[]>`
      SELECT internal_id FROM external_ids_as_of('2026-01-01T00:00:00Z') WHERE external_id = 'R1'`;
    return { code, total: rows[0]?.n, oldKept: old[0]?.internal_id === s.team, past: past[0]?.internal_id, now: now[0]?.internal_id, t1: s.team, t2: s.team2 };
  });
  check("12. superseded + current mapping allowed", remap.code === "none", remap.code);
  check("13. remap preserves the old row (2 rows, old intact)", remap.total === 2 && remap.oldKept);

  console.log("\n  as-of mechanism:");
  check("14. current mapping visible at a current cutoff", remap.now === remap.t2, `${remap.now}`);
  check("15. past cutoff returns the historical mapping", remap.past === remap.t1, `${remap.past}`);
  check("16. superseded row disappears after superseded_at", remap.past !== remap.now);

  const [pred] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM pg_proc p JOIN pg_namespace n2 ON n2.oid = p.pronamespace
     WHERE n2.nspname = 'public' AND p.prokind = 'f'
       AND pg_get_functiondef(p.oid) LIKE ${`%${PREDICATE}%`}`;
  check("19. catalog: exactly ONE object defines the predicate", pred?.n === 1, `found ${pred?.n}`);
  const [deleg] = await sql<{ src: string }[]>`SELECT prosrc AS src FROM pg_proc WHERE proname = 'external_ids_as_of'`;
  check(
    "19b. wrapper delegates to fn_visible_at rather than restating",
    (deleg?.src ?? "").includes("fn_visible_at") && !(deleg?.src ?? "").includes("superseded_at IS NULL OR"),
  );
  const selfSrc = readFileSync(new URL(import.meta.url), "utf8");
  const restatements = selfSrc.split(PREDICATE).length - 1;
  check(
    "19c. this verification file restates the predicate ZERO times",
    restatements === 0,
    `found ${restatements}`,
  );

  console.log("\n  polymorphic integrity (all five registries):");
  const integrity = await inRollback(async (tx) => {
    const s = await seed(tx, "int");
    const out: Record<string, { blocked: string; allowed: string }> = {};
    for (const [type, table] of CANONICAL) {
      const id = s[type as keyof typeof s] as string;
      await tx.unsafe(ins(s, type, `K-${type}`, id));
      out[type] = {
        blocked: await codeOf(tx, `DELETE FROM ${table} WHERE id = '${id}'`),
        allowed: "n/a",
      };
    }
    // unreferenced delete must succeed
    out["unreferenced"] = { blocked: "n/a", allowed: await codeOf(tx, `DELETE FROM teams WHERE id = '${s.team2}'`) };
    return out;
  });
  for (const [type] of CANONICAL) {
    check(`20/22. delete blocked while mapped: ${type}`, integrity[type]?.blocked === "23503", integrity[type]?.blocked);
  }
  check("21. delete allowed when unreferenced", integrity["unreferenced"]?.allowed === "none");

  const safety = await inRollback(async (tx) => {
    const s = await seed(tx, "safe");
    const bad = await codeOf(tx, ins(s, "team", "S1", "99999999-9999-9999-9999-999999999999"));
    const [still] = await tx<{ n: number }[]>`SELECT count(*)::int AS n FROM teams`;
    return { bad, usable: (still?.n ?? 0) > 0 };
  });
  check("23. trigger errors are transaction-safe (savepoint recovery)", safety.bad === "23503" && safety.usable);

  const [lock] = await sql<{ src: string }[]>`SELECT prosrc AS src FROM pg_proc WHERE proname = 'trg_external_ids_integrity'`;
  check("24. mapping-side check uses FOR KEY SHARE", (lock?.src ?? "").includes("FOR KEY SHARE"));

  console.log("\n  grants:");
  const denied = await inRollback(async (tx) => {
    const s = await seed(tx, "grant");
    await tx.unsafe(ins(s, "team", "G1", s.team));
    await tx`INSERT INTO entity_review_queue (entity_type, source_id, external_id, reason)
             VALUES ('team', ${s.source}, 'G1', 'ambiguous')`;
    await tx.unsafe(`SET LOCAL ROLE ${ENGINE}`);
    const r: Record<string, string> = {};
    r.updInternal = await codeOf(tx, `UPDATE external_ids SET internal_id = '${s.team2}'`);
    r.updType = await codeOf(tx, `UPDATE external_ids SET entity_type = 'venue'`);
    r.updExternal = await codeOf(tx, `UPDATE external_ids SET external_id = 'x'`);
    r.updSource = await codeOf(tx, `UPDATE external_ids SET source_id = '${s.source}'`);
    r.updKnown = await codeOf(tx, `UPDATE external_ids SET known_at = now()`);
    r.del = await codeOf(tx, `DELETE FROM external_ids`);
    r.truncEid = await codeOf(tx, `TRUNCATE external_ids`);
    r.truncTeams = await codeOf(tx, `TRUNCATE teams`);
    r.queueDel = await codeOf(tx, `DELETE FROM entity_review_queue`);
    return r;
  });
  for (const [name, code] of Object.entries(denied)) {
    check(`29/26/33. refused: ${name}`, code === "42501", `expected 42501, got ${code}`);
  }

  const allowed = await inRollback(async (tx) => {
    const s = await seed(tx, "allow");
    await tx.unsafe(ins(s, "team", "A1", s.team));
    await tx`INSERT INTO entity_review_queue (entity_type, source_id, external_id, reason)
             VALUES ('team', ${s.source}, 'A1', 'ambiguous')`;
    await tx.unsafe(`SET LOCAL ROLE ${ENGINE}`);
    return {
      supersede: await codeOf(tx, `UPDATE external_ids SET superseded_at = now() + interval '1 second'`),
      confidence: await codeOf(tx, `UPDATE external_ids SET confidence = 55`),
      verified: await codeOf(tx, `UPDATE external_ids SET last_verified_at = now()`),
      insertMap: await codeOf(tx, ins(s, "venue", "A2", s.venue)),
      resolveItem: await codeOf(tx, `UPDATE entity_review_queue SET status='resolved', resolved_at=now(), resolved_by='probe'`),
    };
  });
  for (const [name, code] of Object.entries(allowed)) {
    check(`30. permitted: ${name}`, code === "none", code);
  }

  for (const role of ["app_rw", "analytics_ro"]) {
    const res = await inRollback(async (tx) => {
      await tx.unsafe(`SET LOCAL ROLE ${role}`);
      return {
        read: await codeOf(tx, `SELECT 1 FROM external_ids`),
        readQueue: await codeOf(tx, `SELECT 1 FROM entity_review_queue`),
        write: await codeOf(tx, `INSERT INTO entity_review_queue (entity_type, source_id, external_id, reason) VALUES ('team', gen_random_uuid(), 'x', 'y')`),
        trunc: await codeOf(tx, `TRUNCATE external_ids`),
      };
    });
    check(`31/32. ${role} may SELECT both tables`, res.read === "none" && res.readQueue === "none");
    check(`31/32. ${role} may NOT write`, res.write === "42501", res.write);
    check(`33. ${role} may NOT TRUNCATE`, res.trunc === "42501", res.trunc);
  }

  console.log("\n  entity_review_queue:");
  const [cols] = await sql<{ cols: string }[]>`
    SELECT string_agg(column_name, ',' ORDER BY column_name) AS cols
      FROM information_schema.columns WHERE table_name = 'entity_review_queue'`;
  const required = ["id", "entity_type", "source_id", "external_id", "candidate_internal_id",
    "reason", "status", "created_at", "resolved_at", "resolved_by", "resolution_note"];
  const have = (cols?.cols ?? "").split(",");
  check("27. all §11.2 capability columns exist", required.every((c) => have.includes(c)),
    `missing ${required.filter((c) => !have.includes(c)).join(",")}`);
  check("28a. generic: no score/threshold/algorithm columns",
    !have.some((c) => /score|threshold|algorithm|weight|fuzzy/.test(c)), have.join(","));
  const queue = await inRollback(async (tx) => {
    const s = await seed(tx, "q");
    return {
      badStatus: await codeOf(tx, `INSERT INTO entity_review_queue (entity_type, source_id, external_id, reason, status) VALUES ('team','${s.source}','Q','r','maybe')`),
      openResolved: await codeOf(tx, `INSERT INTO entity_review_queue (entity_type, source_id, external_id, reason, status, resolved_at) VALUES ('team','${s.source}','Q','r','open', now())`),
      nullCandidate: await codeOf(tx, `INSERT INTO entity_review_queue (entity_type, source_id, external_id, reason) VALUES ('team','${s.source}','Q','r')`),
    };
  });
  check("28b. status CHECK rejects an unknown status", queue.badStatus === "23514", queue.badStatus);
  check("28c. an open item may not carry a resolved_at", queue.openResolved === "23514", queue.openResolved);
  check("28d. candidate_internal_id is nullable (no match is the common case)", queue.nullCandidate === "none", queue.nullCandidate);

  const [empty] = await sql<{ n: number }[]>`SELECT count(*)::int AS n FROM entity_review_queue`;
  check("28e. P0-06 leaves the queue EMPTY (P0-12 populates it)", empty?.n === 0, `${empty?.n} rows`);

  console.log(failures === 0 ? "\nall invariants hold" : `\n${failures} invariant(s) violated`);
  if (failures > 0) process.exitCode = 1;
} finally {
  await sql.end({ timeout: 5 });
}
