/**
 * Live PostgreSQL invariant check for the P0-05 canonical entity layer
 * (PHASE-0-SPEC.md §10 and its acceptance criterion).
 *
 * Like db:verify-partitions and unlike db:verify-generate, this inspects the
 * actual database — structure, temporal behaviour and grants. It never reads a
 * TypeScript declaration.
 *
 * Every mutating probe runs in a transaction that is rolled back, so the check
 * leaves no rows behind and is safe to run repeatedly.
 */
import postgres from "postgres";

import { getDatabaseUrl } from "./connection.ts";

const ENGINE_ROLE = "engine_rw";

const TABLES = [
  "countries", "venues", "competitions", "competition_names",
  "seasons", "teams", "team_names", "team_aliases",
];

const PARTIAL_UNIQUES: Record<string, string> = {
  team_names_current_idx: "(team_id, name_type) WHERE (valid_to IS NULL)",
  competition_names_current_idx: "(competition_id, name_type) WHERE (valid_to IS NULL)",
  seasons_one_current_idx: "(competition_id) WHERE is_current",
  team_aliases_global_idx: "(normalized_alias) WHERE (source_id IS NULL)",
  team_aliases_source_idx: "(source_id, normalized_alias) WHERE (source_id IS NOT NULL)",
};

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

/**
 * SQLSTATE of running `stmt`, or "none" when it succeeds.
 *
 * Each probe is wrapped in a SAVEPOINT. Without one, the first expected
 * failure aborts the whole transaction and every later probe returns 25P02
 * ("in failed SQL transaction") instead of its own real error — which makes
 * the checks look like they pass or fail for the wrong reason.
 */
let savepointSeq = 0;
const codeOf = async (tx: postgres.TransactionSql, stmt: string): Promise<string> => {
  const sp = `vc_sp_${(savepointSeq += 1)}`;
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

/** Seeds one country/competition/team and returns their ids. */
const seed = async (tx: postgres.TransactionSql, tag: string) => {
  const [c] = await tx<{ id: string }[]>`
    INSERT INTO countries (slug, name) VALUES (${`c-${tag}`}, 'Testland') RETURNING id`;
  const [comp] = await tx<{ id: string }[]>`
    INSERT INTO competitions (slug, country_id, type, gender)
    VALUES (${`k-${tag}`}, ${c!.id}, 'league', 'men') RETURNING id`;
  const [t] = await tx<{ id: string }[]>`
    INSERT INTO teams (slug, country_id, gender) VALUES (${`t-${tag}`}, ${c!.id}, 'men') RETURNING id`;
  return { countryId: c!.id, competitionId: comp!.id, teamId: t!.id };
};

try {
  console.log("canonical entity layer invariants\n");

  // 1. tables exist
  const present = await sql<{ tablename: string }[]>`
    SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(${TABLES})`;
  check("1. all eight canonical tables exist", present.length === 8, `found ${present.length}`);

  // 2. teams has no name column — the structural guarantee behind §6 rule 7
  const [nameCol] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM information_schema.columns
     WHERE table_name = 'teams' AND column_name = 'name'`;
  check("2. teams has NO name column", nameCol?.n === 0);

  // 3. partial unique indexes exist with the right predicates
  for (const [idx, expect] of Object.entries(PARTIAL_UNIQUES)) {
    const [row] = await sql<{ def: string }[]>`
      SELECT indexdef AS def FROM pg_indexes WHERE schemaname = 'public' AND indexname = ${idx}`;
    const def = row?.def ?? "";
    const norm = (s: string) => s.replace(/\s+/g, " ").toLowerCase();
    check(
      `3. ${idx} is a partial UNIQUE index`,
      def.startsWith("CREATE UNIQUE INDEX") && norm(def).includes(norm(expect)),
      def || "missing",
    );
  }

  // 4. foreign keys
  const fks = await sql<{ src: string; tgt: string }[]>`
    SELECT c.conrelid::regclass::text AS src, c.confrelid::regclass::text AS tgt
      FROM pg_constraint c WHERE c.contype = 'f' AND c.conrelid::regclass::text = ANY(${TABLES})`;
  const has = (s: string, t: string) => fks.some((f) => f.src === s && f.tgt === t);
  check(
    "4. all ten canonical foreign keys present",
    has("competitions", "countries") && has("venues", "countries") &&
      has("seasons", "competitions") && has("competition_names", "competitions") &&
      has("teams", "countries") && has("teams", "teams") &&
      has("team_names", "teams") && has("team_aliases", "teams") &&
      has("team_aliases", "data_sources"),
    `found ${fks.length}`,
  );
  check("4b. nothing in P0-05 references a partitioned table", !fks.some((f) => f.tgt === "raw_payloads"));

  // 5. CHECK constraints and defaults
  const [checks] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM pg_constraint
     WHERE contype = 'c' AND conrelid::regclass::text = ANY(${TABLES})`;
  check("5. CHECK constraints present (>= 25)", (checks?.n ?? 0) >= 25, `found ${checks?.n}`);

  const rejects = await inRollback(async (tx) => {
    const s = await seed(tx, "chk");
    return {
      gender: await codeOf(tx, `INSERT INTO teams (slug, country_id, gender) VALUES ('bad-g', '${s.countryId}', 'other')`),
      status: await codeOf(tx, `INSERT INTO teams (slug, country_id, gender, status) VALUES ('bad-s', '${s.countryId}', 'men', 'zombie')`),
      interval: await codeOf(tx, `INSERT INTO team_names (team_id, name, name_type, valid_from, valid_to) VALUES ('${s.teamId}', 'X', 'official', '2020-01-01', '2019-01-01')`),
      aliasCase: await codeOf(tx, `INSERT INTO team_aliases (team_id, alias, normalized_alias) VALUES ('${s.teamId}', 'X', 'NotLower')`),
      selfParent: await codeOf(tx, `UPDATE teams SET parent_team_id = id WHERE id = '${s.teamId}'`),
    };
  });
  check("5a. teams.gender CHECK rejects an invalid value", rejects.gender === "23514", rejects.gender);
  check("5b. teams.status CHECK rejects an invalid value", rejects.status === "23514", rejects.status);
  check("5c. team_names interval CHECK rejects valid_to <= valid_from", rejects.interval === "23514", rejects.interval);
  check("5d. team_aliases CHECK rejects a non-lowercase normalized_alias", rejects.aliasCase === "23514", rejects.aliasCase);
  check("5e. teams CHECK rejects self-parenting", rejects.selfParent === "23514", rejects.selfParent);

  // 6. the acceptance criterion: a rename resolves correctly at both dates
  const rename = await inRollback(async (tx) => {
    const s = await seed(tx, "rename");
    await tx`INSERT INTO team_names (team_id, name, name_type, valid_from, valid_to) VALUES
      (${s.teamId}, 'Guangzhou Evergrande Taobao', 'official', '2016-01-01', '2021-01-01'),
      (${s.teamId}, 'Guangzhou FC',                'official', '2021-01-01', NULL)`;
    const asOf = async (d: string) => {
      const [r] = await tx<{ name: string }[]>`
        SELECT name FROM team_names WHERE team_id = ${s.teamId} AND name_type = 'official'
          AND valid_from <= ${d}::date AND (valid_to IS NULL OR valid_to > ${d}::date)`;
      return r?.name;
    };
    return { historical: await asOf("2019-05-01"), current: await asOf("2026-09-08") };
  });
  check(
    "6. rename resolves: 2019 -> historical name, today -> current name",
    rename.historical === "Guangzhou Evergrande Taobao" && rename.current === "Guangzhou FC",
    `${rename.historical} / ${rename.current}`,
  );

  // 7. temporal uniqueness + the documented two-step handover
  const handover = await inRollback(async (tx) => {
    const s = await seed(tx, "hand");
    await tx`INSERT INTO team_names (team_id, name, name_type, valid_from) VALUES (${s.teamId}, 'Original FC', 'official', '2010-01-01')`;
    const second = await codeOf(tx, `INSERT INTO team_names (team_id, name, name_type, valid_from) VALUES ('${s.teamId}', 'Impostor FC', 'official', '2020-01-01')`);
    const otherType = await codeOf(tx, `INSERT INTO team_names (team_id, name, name_type, valid_from) VALUES ('${s.teamId}', 'Original', 'common', '2010-01-01')`);
    // step 1: close, step 2: open — the documented ordered handover (§10.4)
    const close = await codeOf(tx, `UPDATE team_names SET valid_to = '2024-01-01' WHERE team_id = '${s.teamId}' AND name_type = 'official' AND valid_to IS NULL`);
    const open = await codeOf(tx, `INSERT INTO team_names (team_id, name, name_type, valid_from) VALUES ('${s.teamId}', 'Renamed FC', 'official', '2024-01-01')`);
    const [n] = await tx<{ n: number }[]>`SELECT count(*)::int AS n FROM team_names WHERE team_id = ${s.teamId} AND name_type = 'official'`;
    return { second, otherType, close, open, total: n?.n };
  });
  check("7a. second CURRENT name for one (team, name_type) is rejected", handover.second === "23505", handover.second);
  check("7b. a current name of a different name_type is allowed", handover.otherType === "none", handover.otherType);
  check("7c. handover step 1 (close) succeeds", handover.close === "none", handover.close);
  check("7d. handover step 2 (open replacement) succeeds", handover.open === "none", handover.open);
  check("7e. handover leaves 2 official rows (1 closed, 1 current)", handover.total === 2, `${handover.total}`);

  // 8. the same for current seasons
  const seasonHandover = await inRollback(async (tx) => {
    const s = await seed(tx, "season");
    await tx`INSERT INTO seasons (slug, competition_id, label, start_year, is_current) VALUES ('s1', ${s.competitionId}, '2025/26', 2025, true)`;
    await tx`INSERT INTO seasons (slug, competition_id, label, start_year, is_current) VALUES ('s2', ${s.competitionId}, '2026/27', 2026, false)`;
    const secondCurrent = await codeOf(tx, `INSERT INTO seasons (slug, competition_id, label, start_year, is_current) VALUES ('s3', '${s.competitionId}', '2027/28', 2027, true)`);
    // A single-statement flip is ORDER-DEPENDENT: it raises 23505 only when
    // PostgreSQL happens to set the new current row before clearing the old
    // one. Asserting deterministic failure would be asserting physical row
    // order. What is guaranteed is the invariant: at most one current season
    // survives either way. That is why the documented handover uses two
    // ordered statements — the one-shot form is unreliable, not reliably safe.
    const oneShot = await codeOf(tx, `UPDATE seasons SET is_current = (label = '2026/27') WHERE competition_id = '${s.competitionId}'`);
    const [afterOneShot] = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM seasons WHERE competition_id = ${s.competitionId} AND is_current`;
    const close = await codeOf(tx, `UPDATE seasons SET is_current = false WHERE competition_id = '${s.competitionId}' AND label = '2025/26'`);
    const open = await codeOf(tx, `UPDATE seasons SET is_current = true WHERE competition_id = '${s.competitionId}' AND label = '2026/27'`);
    const [cur] = await tx<{ label: string }[]>`SELECT label FROM seasons WHERE competition_id = ${s.competitionId} AND is_current`;
    return { secondCurrent, oneShot, oneShotCurrent: afterOneShot?.n, close, open, current: cur?.label };
  });
  check("8a. second current season is rejected", seasonHandover.secondCurrent === "23505", seasonHandover.secondCurrent);
  check(
    "8b. single-statement flip: invariant holds either way (<=1 current)",
    (seasonHandover.oneShot === "23505" || seasonHandover.oneShot === "none") &&
      (seasonHandover.oneShotCurrent ?? 0) <= 1,
    `code=${seasonHandover.oneShot} current=${seasonHandover.oneShotCurrent}`,
  );
  check("8c. ordered two-step season handover succeeds", seasonHandover.close === "none" && seasonHandover.open === "none");
  check("8d. exactly one current season afterwards, the new one", seasonHandover.current === "2026/27", seasonHandover.current);

  // 9. alias ambiguity must fail rather than resolve arbitrarily
  const alias = await inRollback(async (tx) => {
    const a = await seed(tx, "al-a");
    const b = await seed(tx, "al-b");
    await tx`INSERT INTO team_aliases (team_id, alias, normalized_alias) VALUES (${a.teamId}, 'Barcelona', 'barcelona')`;
    const clash = await codeOf(tx, `INSERT INTO team_aliases (team_id, alias, normalized_alias) VALUES ('${b.teamId}', 'Barcelona', 'barcelona')`);
    const [src] = await tx<{ id: string }[]>`INSERT INTO data_sources (slug, display_name) VALUES ('prov-a', 'Provider A') RETURNING id`;
    const scoped = await codeOf(tx, `INSERT INTO team_aliases (team_id, alias, normalized_alias, source_id) VALUES ('${b.teamId}', 'Barcelona', 'barcelona', '${src!.id}')`);
    return { clash, scoped };
  });
  check("9a. two teams cannot share a GLOBAL alias (ambiguity fails)", alias.clash === "23505", alias.clash);
  check("9b. a provider-scoped alias may reuse the same string", alias.scoped === "none", alias.scoped);

  // 10. grants — negative tests
  console.log("\n  grants (engine_rw must be refused):");
  const denied = await inRollback(async (tx) => {
    const s = await seed(tx, "grant");
    await tx`INSERT INTO team_names (team_id, name, name_type, valid_from) VALUES (${s.teamId}, 'N', 'official', '2020-01-01')`;
    await tx`INSERT INTO competition_names (competition_id, name, name_type, valid_from) VALUES (${s.competitionId}, 'C', 'official', '2020-01-01')`;
    await tx.unsafe(`SET LOCAL ROLE ${ENGINE_ROLE}`);
    return {
      nameEdit: await codeOf(tx, `UPDATE team_names SET name = 'Rewritten'`),
      nameFrom: await codeOf(tx, `UPDATE team_names SET valid_from = '1999-01-01'`),
      nameDelete: await codeOf(tx, `DELETE FROM team_names`),
      compEdit: await codeOf(tx, `UPDATE competition_names SET name = 'Rewritten'`),
      teamDelete: await codeOf(tx, `DELETE FROM teams`),
      seasonDelete: await codeOf(tx, `DELETE FROM seasons`),
      compDelete: await codeOf(tx, `DELETE FROM competitions`),
    };
  });
  for (const [name, code] of Object.entries(denied)) {
    check(`10. refused: ${name}`, code === "42501", `expected 42501, got ${code}`);
  }

  console.log("\n  grants (engine_rw must succeed):");
  const allowed = await inRollback(async (tx) => {
    const s = await seed(tx, "allow");
    await tx`INSERT INTO team_names (team_id, name, name_type, valid_from) VALUES (${s.teamId}, 'N', 'official', '2020-01-01')`;
    await tx.unsafe(`SET LOCAL ROLE ${ENGINE_ROLE}`);
    return {
      closeName: await codeOf(tx, `UPDATE team_names SET valid_to = '2024-01-01'`),
      closeCompName: await codeOf(tx, `UPDATE competition_names SET valid_to = '2024-01-01'`),
      tombstoneTeam: await codeOf(tx, `UPDATE teams SET status = 'dissolved'`),
      updateSeason: await codeOf(tx, `UPDATE seasons SET is_current = false`),
      deleteAlias: await codeOf(tx, `DELETE FROM team_aliases`),
    };
  });
  for (const [name, code] of Object.entries(allowed)) {
    check(`11. permitted: ${name}`, code === "none", code);
  }

  // 12. app_rw and analytics_ro are read-only here
  console.log("\n  app_rw / analytics_ro are read-only over the canonical layer:");
  for (const role of ["app_rw", "analytics_ro"]) {
    const res = await inRollback(async (tx) => {
      await tx.unsafe(`SET LOCAL ROLE ${role}`);
      const read = await codeOf(tx, `SELECT 1 FROM teams`);
      const write = await codeOf(tx, `INSERT INTO countries (slug, name) VALUES ('x', 'X')`);
      return { read, write };
    });
    check(`12. ${role} may SELECT`, res.read === "none", res.read);
    check(`12. ${role} may NOT INSERT`, res.write === "42501", res.write);
  }

  console.log(failures === 0 ? "\nall invariants hold" : `\n${failures} invariant(s) violated`);
  if (failures > 0) process.exitCode = 1;
} finally {
  await sql.end({ timeout: 5 });
}
