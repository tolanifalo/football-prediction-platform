/**
 * Live PostgreSQL invariant check for the P0-08 match fact layer
 * (PHASE-0-SPEC.md §13 and the §E P0-08 acceptance criterion).
 *
 * Inspects the database, never a TypeScript declaration. Mutating probes run
 * inside rolled-back transactions with SAVEPOINTs, so one expected failure
 * cannot poison the rest (25P02, the defect verify-canonical hit). No fixed
 * identifiers: repeated runs must be identical.
 *
 * Positive outcomes are asserted on VALUES. An as-of read that returns nothing
 * is not evidence that it returned the right revision.
 *
 * NOTE ON THE AS-OF PREDICATE: this file must never restate the bitemporal
 * visibility comparison. Every as-of read goes through match_results_as_of(),
 * match_stats_as_of() or fn_visible_at(). One check asserts the database holds
 * exactly one definition across all FOUR wrappers; another that each new
 * wrapper delegates; another that this file restates it zero times - the probe
 * string is assembled from fragments (§11.1).
 */
import { readFileSync } from "node:fs";

import postgres from "postgres";

import { getDatabaseUrl } from "./connection.ts";

const ENGINE = "engine_rw";
/** Assembled from fragments so the literal never appears whole in this file. */
const PREDICATE = ["superseded_at IS NULL OR", "superseded_at >"].join(" ");

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
/** SQLSTATE of `stmt`, or "none". Savepoint-isolated. */
const codeOf = async (tx: postgres.TransactionSql, stmt: string): Promise<string> => {
  const sp = `vfa_sp_${(spSeq += 1)}`;
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

/** Two data sources, a payload body, and one fixture with a schedule revision. */
const seed = async (tx: postgres.TransactionSql, tag: string) => {
  const [a] = await tx<{ id: string }[]>`
    INSERT INTO data_sources (slug, display_name) VALUES (${`sa-${tag}`}, 'A') RETURNING id`;
  const [b] = await tx<{ id: string }[]>`
    INSERT INTO data_sources (slug, display_name) VALUES (${`sb-${tag}`}, 'B') RETURNING id`;
  const [body] = await tx<{ id: string }[]>`
    INSERT INTO raw_payload_bodies (body_hash, byte_size)
    VALUES (${"e".repeat(64)}, 0) RETURNING id`;
  const [c] = await tx<{ id: string }[]>`
    INSERT INTO countries (slug, name) VALUES (${`c-${tag}`}, 'Probeland') RETURNING id`;
  const [comp] = await tx<{ id: string }[]>`
    INSERT INTO competitions (slug, country_id, type, gender)
    VALUES (${`k-${tag}`}, ${c!.id}, 'league', 'men') RETURNING id`;
  const [se] = await tx<{ id: string }[]>`
    INSERT INTO seasons (slug, competition_id, label, start_year)
    VALUES (${`se-${tag}`}, ${comp!.id}, '2025/26', 2025) RETURNING id`;
  const [h] = await tx<{ id: string }[]>`
    INSERT INTO teams (slug, country_id, gender) VALUES (${`th-${tag}`}, ${c!.id}, 'men') RETURNING id`;
  const [aw] = await tx<{ id: string }[]>`
    INSERT INTO teams (slug, country_id, gender) VALUES (${`ta-${tag}`}, ${c!.id}, 'men') RETURNING id`;
  return { srcA: a!.id, srcB: b!.id, body: body!.id, season: se!.id, home: h!.id, away: aw!.id };
};

type S = Awaited<ReturnType<typeof seed>>;

/** A fresh fixture plus its mandatory first schedule revision (§12.4, D10). */
const newFixture = async (tx: postgres.TransactionSql, s: S, stage: string): Promise<string> => {
  const [f] = await tx<{ id: string }[]>`
    INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
    VALUES (${s.season}, ${stage}, ${s.home}, ${s.away}) RETURNING id`;
  await tx`
    INSERT INTO fixture_schedule
      (fixture_id, kickoff_utc, local_date, local_tz, status, source_id, raw_payload_body_id)
    VALUES (${f!.id}, ${"2026-02-01 15:00Z"}, ${"2026-02-01"}, 'UTC', 'ft', ${s.srcA}, ${s.body})`;
  return f!.id;
};

const res = (
  s: S,
  fixtureId: string,
  cols: string,
  vals: string,
  opts: { source?: string; knownAt?: string; supersededAt?: string; src?: string; trainable?: boolean } = {},
): string =>
  `INSERT INTO match_results
     (fixture_id, result_source, is_trainable, source_id, raw_payload_body_id, known_at${
       opts.supersededAt ? ", superseded_at" : ""
     }, ${cols})
   VALUES ('${fixtureId}', '${opts.src ?? "played"}', ${opts.trainable ?? true},
           '${opts.source ?? s.srcA}', ${s.body}, timestamptz '${opts.knownAt ?? "2026-02-01 18:00Z"}'${
             opts.supersededAt ? `, timestamptz '${opts.supersededAt}'` : ""
           }, ${vals})`;

const stats = (s: S, fixtureId: string, cols: string, vals: string, source?: string): string =>
  `INSERT INTO match_stats (fixture_id, source_id, raw_payload_body_id${cols ? ", " + cols : ""})
   VALUES ('${fixtureId}', '${source ?? s.srcA}', ${s.body}${vals ? ", " + vals : ""})`;

const planOf = async (tx: postgres.TransactionSql, query: string): Promise<string> => {
  const rows = await tx.unsafe(`EXPLAIN (COSTS OFF, FORMAT TEXT) ${query}`);
  return (rows as unknown as { "QUERY PLAN": string }[]).map((r) => r["QUERY PLAN"]).join("\n");
};

const cols = async (table: string): Promise<Map<string, string>> => {
  const rows = await sql<{ column_name: string; is_nullable: string }[]>`
    SELECT column_name, is_nullable FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = ${table}`;
  return new Map(rows.map((r) => [r.column_name, r.is_nullable]));
};

try {
  console.log("match fact invariants\n");

  // =====================================================================
  console.log("  match_results — scoreline and administrative constraints:");
  const RCASES: [string, string, string, string, Record<string, unknown>][] = [
    ["1.  ft 3-0, played, trainable", "ACCEPT", "ft_home, ft_away", "3, 0", {}],
    ["2.  negative score", "REJECT", "ft_home, ft_away", "-1, 0", {}],
    ["3.  half-time exceeds full-time", "REJECT", "ft_home, ft_away, ht_home, ht_away", "1, 0, 2, 0", {}],
    ["4.  half-time 1-0 inside full-time 2-1", "ACCEPT", "ft_home, ft_away, ht_home, ht_away", "2, 1, 1, 0", {}],
    ["5.  only one half of the ht pair", "REJECT", "ft_home, ft_away, ht_home", "2, 1, 1", {}],
    ["6.  aet BELOW ft (aet is CUMULATIVE)", "REJECT", "ft_home, ft_away, aet_home, aet_away", "2, 2, 1, 2", {}],
    ["7.  aet 3-2 after ft 2-2", "ACCEPT", "ft_home, ft_away, aet_home, aet_away", "2, 2, 3, 2", {}],
    ["8.  only one half of the aet pair", "REJECT", "ft_home, ft_away, aet_home", "2, 2, 3", {}],
    ["9.  penalties without extra time", "REJECT", "ft_home, ft_away, pens_home, pens_away", "1, 1, 4, 3", {}],
    ["10. a DRAWN shootout", "REJECT", "ft_home, ft_away, aet_home, aet_away, pens_home, pens_away", "1, 1, 1, 1, 4, 4", {}],
    ["11. ft 1-1, aet 1-1, pens 4-3", "ACCEPT", "ft_home, ft_away, aet_home, aet_away, pens_home, pens_away", "1, 1, 1, 1, 4, 3", {}],
    ["12. goalless draw 0-0 (zero is a real score)", "ACCEPT", "ft_home, ft_away, ht_home, ht_away", "0, 0, 0, 0", {}],
    ["13. AWARDED 3-0 marked trainable", "REJECT", "ft_home, ft_away", "3, 0", { src: "awarded", trainable: true }],
    ["14. AWARDED 3-0 marked untrainable", "ACCEPT", "ft_home, ft_away", "3, 0", { src: "awarded", trainable: false }],
    ["15. unknown result_source", "REJECT", "ft_home, ft_away", "3, 0", { src: "abandoned" }],
    ["16. superseded_at equal to known_at", "REJECT", "ft_home, ft_away", "1, 0",
      { knownAt: "2026-02-01 18:00Z", supersededAt: "2026-02-01 18:00Z" }],
  ];
  const rOut = await inRollback(async (tx) => {
    const s = await seed(tx, "r");
    const out: Record<string, string> = {};
    for (const [label, , c, v, opts] of RCASES) {
      const f = await newFixture(tx, s, `r-${label.split(".")[0]}`);
      out[label] = await codeOf(tx, res(s, f, c, v, opts as never));
    }
    return { codes: out };
  });
  for (const [label, expect] of RCASES) {
    const code = rOut.codes[label];
    check(
      `${label}  MUST ${expect}`,
      expect === "ACCEPT" ? code === "none" : code === "23514",
      code,
    );
  }

  // =====================================================================
  console.log("\n  abandoned fixtures — the E5 ruling (§13.4):");
  const [srcDef] = await sql<{ def: string }[]>`
    SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
     WHERE conname = 'match_results_source_check'`;
  check(
    "17. result_source permits EXACTLY 'played' and 'awarded' — no third value",
    !!srcDef?.def && srcDef.def.includes("'played'") && srcDef.def.includes("'awarded'") &&
      !/abandoned|partial|void|walkover|forfeit/i.test(srcDef.def),
    srcDef?.def ?? "absent",
  );
  const [awDef] = await sql<{ def: string }[]>`
    SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
     WHERE conname = 'match_results_awarded_untrainable_check'`;
  check(
    "18. awarded ⇒ never trainable, enforced by CHECK not by convention",
    !!awDef?.def && /awarded/.test(awDef.def) && /is_trainable/.test(awDef.def),
    awDef?.def ?? "absent",
  );
  // The E5 ruling is a POLICY, not a constraint: nothing in the schema can stop
  // an ingester writing a partial score. What the schema DOES express is checks
  // 17 and 18 above. This probe asserts the observable state the policy
  // produces, and is explicit about which half is unenforced.
  const abandoned = await inRollback(async (tx) => {
    const s = await seed(tx, "ab");
    const f = await newFixture(tx, s, "abandoned-probe");
    // Really abandon it: close the current revision, then open the new one.
    await tx`UPDATE fixture_schedule SET superseded_at = now() + interval '1 second'
              WHERE fixture_id = ${f} AND superseded_at IS NULL`;
    await tx`
      INSERT INTO fixture_schedule
        (fixture_id, kickoff_utc, local_date, local_tz, status, source_id, raw_payload_body_id, known_at)
      VALUES (${f}, ${"2026-02-01 15:00Z"}, ${"2026-02-01"}, 'UTC', 'abandoned', ${s.srcA}, ${s.body},
              now() + interval '2 seconds')`;
    const [status] = await tx<{ status: string }[]>`
      SELECT status FROM fixture_schedule WHERE fixture_id = ${f} AND superseded_at IS NULL`;
    const [noResult] = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM match_results WHERE fixture_id = ${f}`;
    const [evidence] = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM raw_payload_bodies WHERE id = ${s.body}`;
    // Only an explicitly awarded result becomes canonical, and only as untrainable.
    const awarded = await codeOf(
      tx, res(s, f, "ft_home, ft_away", "3, 0", { src: "awarded", trainable: false }));
    const awardedTrainable = await codeOf(
      tx, res(s, f, "ft_home, ft_away", "3, 0", { src: "awarded", trainable: true, source: s.srcB }));
    return { status: status!.status, noResult: noResult!.n, evidence: evidence!.n,
             awarded, awardedTrainable };
  });
  check("19. the probe really did abandon the fixture", abandoned.status === "abandoned", abandoned.status);
  check(
    "20. an abandoned fixture carries NO match_results row from its partial score",
    abandoned.noResult === 0,
    `${abandoned.noResult} row(s)`,
  );
  check(
    "21. …while the observation survives in the raw provenance layer as evidence",
    abandoned.evidence === 1,
    `${abandoned.evidence}`,
  );
  check(
    "22. an officially AWARDED 3-0 is accepted, as awarded + untrainable",
    abandoned.awarded === "none",
    abandoned.awarded,
  );
  check(
    "23. …and the same row marked trainable is rejected by the database",
    abandoned.awardedTrainable === "23514",
    abandoned.awardedTrainable,
  );

  // =====================================================================
  console.log("\n  multi-source truth (§13.5) — the business key:");
  const multi = await inRollback(async (tx) => {
    const s = await seed(tx, "ms");
    const f = await newFixture(tx, s, "multi-source");
    const a = await codeOf(tx, res(s, f, "ft_home, ft_away", "2, 1", { source: s.srcA }));
    const b = await codeOf(tx, res(s, f, "ft_home, ft_away", "2, 2", { source: s.srcB }));
    const dup = await codeOf(tx, res(s, f, "ft_home, ft_away", "9, 9", { source: s.srcA }));
    const rows = await tx<{ ft_home: number; ft_away: number }[]>`
      SELECT ft_home, ft_away FROM match_results
       WHERE fixture_id = ${f} AND superseded_at IS NULL ORDER BY ft_away`;
    return { a, b, dup, rows };
  });
  check("24. provider A's result is accepted", multi.a === "none", multi.a);
  check("25. provider B may assert the SAME fixture — disagreement is preserved", multi.b === "none", multi.b);
  check(
    "26. …and both current rows are visible (2-1 and 2-2)",
    multi.rows.length === 2 && multi.rows[0]?.ft_away === 1 && multi.rows[1]?.ft_away === 2,
    multi.rows.map((r) => `${r.ft_home}-${r.ft_away}`).join(" "),
  );
  check("27. a SAME-SOURCE second current row is REJECTED", multi.dup === "23505", multi.dup);

  // =====================================================================
  console.log("\n  the §2.3 worked example — the P0-08 acceptance criterion:");
  const T1 = "2026-03-14 17:05Z";
  const T2 = "2026-03-15 03:00Z";
  const T3 = "2026-03-16 11:20Z";
  const worked = await inRollback(async (tx) => {
    const s = await seed(tx, "wk");
    const f = await newFixture(tx, s, "everton-liverpool");
    // T1: provider reports 2-1. T3: corrected to 2-2. Nothing is overwritten.
    await tx.unsafe(
      res(s, f, "ft_home, ft_away, occurred_at", "2, 1, timestamptz '2026-03-14 16:50Z'",
        { knownAt: T1, supersededAt: T3 }),
    );
    await tx.unsafe(
      res(s, f, "ft_home, ft_away, occurred_at", "2, 2, timestamptz '2026-03-14 16:50Z'",
        { knownAt: T3 }),
    );
    const atT2 = await tx<{ ft_home: number; ft_away: number; occurred_at: Date }[]>`
      SELECT ft_home, ft_away, occurred_at FROM match_results_as_of(${T2}::timestamptz)
       WHERE fixture_id = ${f}`;
    const atNow = await tx<{ ft_home: number; ft_away: number; occurred_at: Date }[]>`
      SELECT ft_home, ft_away, occurred_at FROM match_results_as_of(now()) WHERE fixture_id = ${f}`;
    const [kept] = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM match_results WHERE fixture_id = ${f}`;
    const beforeT1 = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM match_results_as_of(${"2026-03-01 00:00Z"}::timestamptz)
       WHERE fixture_id = ${f}`;
    const agree = await tx<{ w: number; d: number }[]>`
      SELECT (SELECT count(*)::int FROM match_results_as_of(c.cutoff) WHERE fixture_id = ${f}) AS w,
             (SELECT count(*)::int FROM match_results
               WHERE fixture_id = ${f} AND fn_visible_at(known_at, superseded_at, c.cutoff)) AS d
        FROM (VALUES (${"2026-03-01Z"}::timestamptz), (${T1}::timestamptz), (${T2}::timestamptz),
                     (${T3}::timestamptz), (now())) AS c(cutoff)`;
    return { atT2, atNow, kept: kept!.n, beforeT1: beforeT1[0]!.n, agree };
  });
  check(
    "25. as-of T2 (the prediction cutoff) returns 2-1 — what we actually knew",
    worked.atT2[0]?.ft_home === 2 && worked.atT2[0]?.ft_away === 1,
    `${worked.atT2[0]?.ft_home}-${worked.atT2[0]?.ft_away}`,
  );
  check(
    "26. as-of now returns 2-2 — the corrected truth",
    worked.atNow[0]?.ft_home === 2 && worked.atNow[0]?.ft_away === 2,
    `${worked.atNow[0]?.ft_home}-${worked.atNow[0]?.ft_away}`,
  );
  check("27. exactly one revision is visible at each cutoff", worked.atT2.length === 1 && worked.atNow.length === 1);
  check("28. BOTH revisions survive — nothing was overwritten", worked.kept === 2, `${worked.kept}`);
  check("29. a cutoff before T1 returns NOTHING", worked.beforeT1 === 0, `${worked.beforeT1}`);
  check(
    "30. both revisions share occurred_at — the match happened once",
    worked.atT2[0]?.occurred_at?.toISOString() === worked.atNow[0]?.occurred_at?.toISOString(),
    `${String(worked.atT2[0]?.occurred_at)} vs ${String(worked.atNow[0]?.occurred_at)}`,
  );
  check(
    "31. wrapper and fn_visible_at agree at all 5 cutoffs",
    worked.agree.length === 5 && worked.agree.every((r) => r.w === r.d),
    worked.agree.map((r) => `${r.w}/${r.d}`).join(" "),
  );

  // =====================================================================
  console.log("\n  match_stats — shape and constraints:");
  const statCols = await cols("match_stats");
  for (const forbidden of ["team_id", "is_home", "xga", "home_xga", "away_xga"]) {
    check(`32. match_stats has NO ${forbidden} column`, !statCols.has(forbidden));
  }
  const SCASES: [string, string, string, string][] = [
    ["33. possession 55.00 / 45.00", "ACCEPT", "home_possession, away_possession", "55.00, 45.00"],
    ["34. possession 55.00 / 30.00 (pair ≠ 100)", "REJECT", "home_possession, away_possession", "55.00, 30.00"],
    ["35. both possessions NULL (not provided)", "ACCEPT", "", ""],
    ["36. shots_on_target above shots, home", "REJECT", "home_shots, home_shots_on_target", "3, 9"],
    ["37. shots_on_target above shots, away", "REJECT", "away_shots, away_shots_on_target", "3, 9"],
    ["38. shots 12, on target 5", "ACCEPT", "home_shots, home_shots_on_target", "12, 5"],
    ["39. negative corners", "REJECT", "home_corners", "-1"],
    ["40. negative xg", "REJECT", "home_xg", "-0.500"],
    ["41. xg 1.750 / 0.900", "ACCEPT", "home_xg, away_xg", "1.750, 0.900"],
    ["42. a full plausible stat row", "ACCEPT",
      "home_shots, away_shots, home_shots_on_target, away_shots_on_target, home_corners, away_corners, home_fouls, away_fouls, home_yellow_cards, away_yellow_cards, home_red_cards, away_red_cards, home_possession, away_possession, home_xg, away_xg",
      "14, 8, 6, 2, 7, 3, 11, 14, 2, 3, 0, 1, 58.50, 41.50, 2.150, 0.780"],
  ];
  const sOut = await inRollback(async (tx) => {
    const s = await seed(tx, "st");
    const out: Record<string, string> = {};
    for (const [label, , c, v] of SCASES) {
      const f = await newFixture(tx, s, `s-${label.split(".")[0]}`);
      out[label] = await codeOf(tx, stats(s, f, c, v));
    }
    return { codes: out };
  });
  for (const [label, expect] of SCASES) {
    const code = sOut.codes[label];
    check(`${label}  MUST ${expect}`, expect === "ACCEPT" ? code === "none" : code === "23514", code);
  }

  const zeroNull = await inRollback(async (tx) => {
    const s = await seed(tx, "zn");
    const f = await newFixture(tx, s, "zero-vs-null");
    await tx.unsafe(stats(s, f, "home_shots, home_corners", "0, 0"));
    const [row] = await tx<{ hs: number | null; hc: number | null; as_: number | null }[]>`
      SELECT home_shots AS hs, home_corners AS hc, away_shots AS as_
        FROM match_stats WHERE fixture_id = ${f}`;
    return row!;
  });
  check(
    "43. zero is stored as zero, and absent is stored as NULL — never conflated",
    zeroNull.hs === 0 && zeroNull.hc === 0 && zeroNull.as_ === null,
    `home_shots=${zeroNull.hs} away_shots=${String(zeroNull.as_)}`,
  );

  // =====================================================================
  console.log("\n  independent revisioning (§2.1) and provenance (§1.3):");
  const indep = await inRollback(async (tx) => {
    const s = await seed(tx, "ind");
    const f = await newFixture(tx, s, "independent");
    await tx.unsafe(res(s, f, "ft_home, ft_away", "1, 0", { knownAt: "2026-02-01 18:00Z" }));
    // Stats revise LATER and on their own timeline; the result is untouched.
    await tx.unsafe(
      `INSERT INTO match_stats (fixture_id, source_id, raw_payload_body_id, home_xg, away_xg, known_at, superseded_at)
       VALUES ('${f}', '${s.srcA}', ${s.body}, 1.200, 0.400,
               timestamptz '2026-02-01 19:00Z', timestamptz '2026-02-04 09:00Z')`,
    );
    await tx.unsafe(
      `INSERT INTO match_stats (fixture_id, source_id, raw_payload_body_id, home_xg, away_xg, known_at)
       VALUES ('${f}', '${s.srcA}', ${s.body}, 1.310, 0.450, timestamptz '2026-02-04 09:00Z')`,
    );
    const early = await tx<{ xg: string; ft: number }[]>`
      SELECT (SELECT home_xg FROM match_stats_as_of(${"2026-02-02 00:00Z"}::timestamptz)
               WHERE fixture_id = ${f}) AS xg,
             (SELECT ft_home FROM match_results_as_of(${"2026-02-02 00:00Z"}::timestamptz)
               WHERE fixture_id = ${f}) AS ft`;
    const late = await tx<{ xg: string; ft: number }[]>`
      SELECT (SELECT home_xg FROM match_stats_as_of(now()) WHERE fixture_id = ${f}) AS xg,
             (SELECT ft_home FROM match_results_as_of(now()) WHERE fixture_id = ${f}) AS ft`;
    const [resRevs] = await tx<{ n: number }[]>`
      SELECT count(*)::int AS n FROM match_results WHERE fixture_id = ${f}`;
    return { early: early[0]!, late: late[0]!, resRevs: resRevs!.n };
  });
  check(
    "44. stats revise independently: xG 1.200 → 1.310 across the cutoff",
    Number(indep.early.xg) === 1.2 && Number(indep.late.xg) === 1.31,
    `${indep.early.xg} → ${indep.late.xg}`,
  );
  check(
    "45. …while the result is unchanged and un-revised",
    indep.early.ft === 1 && indep.late.ft === 1 && indep.resRevs === 1,
    `ft ${indep.early.ft}/${indep.late.ft}, ${indep.resRevs} revision(s)`,
  );

  const resCols = await cols("match_results");
  for (const [table, map] of [["match_results", resCols], ["match_stats", statCols]] as const) {
    for (const c of ["source_id", "raw_payload_body_id", "known_at"]) {
      check(`46. ${table}.${c} exists and is NOT NULL`, map.get(c) === "NO", map.get(c) ?? "absent");
    }
    check(`47. ${table}.superseded_at is nullable (NULL = current belief)`, map.get("superseded_at") === "YES");
  }
  check("48. match_results uses occurred_at, not settled_at",
    resCols.has("occurred_at") && !resCols.has("settled_at"));
  check("49. match_results has NO revision column", !resCols.has("revision"));
  check("50. match_results stores no winner", !resCols.has("winner") && !resCols.has("outcome"));
  check("51. neither fact table carries competition_id",
    !resCols.has("competition_id") && !statCols.has("competition_id"));

  // =====================================================================
  console.log("\n  grants — engine_rw:");
  const eng = await inRollback(async (tx) => {
    const s = await seed(tx, "ge");
    const f = await newFixture(tx, s, "grants");
    await tx.unsafe(`SET LOCAL ROLE ${ENGINE}`);
    const insertResult = await codeOf(tx, res(s, f, "ft_home, ft_away", "1, 0"));
    const insertStats = await codeOf(tx, stats(s, f, "home_shots", "9"));
    return {
      insertResult,
      insertStats,
      supersedeResult: await codeOf(tx, `UPDATE match_results SET superseded_at = now() + interval '1 second'`),
      supersedeStats: await codeOf(tx, `UPDATE match_stats SET superseded_at = now() + interval '1 second'`),
      updFtHome: await codeOf(tx, `UPDATE match_results SET ft_home = 9`),
      updFtAway: await codeOf(tx, `UPDATE match_results SET ft_away = 9`),
      updHtHome: await codeOf(tx, `UPDATE match_results SET ht_home = 9`),
      updPens: await codeOf(tx, `UPDATE match_results SET pens_home = 9`),
      updTrainable: await codeOf(tx, `UPDATE match_results SET is_trainable = false`),
      updResultSource: await codeOf(tx, `UPDATE match_results SET result_source = 'awarded'`),
      updKnownAt: await codeOf(tx, `UPDATE match_results SET known_at = now()`),
      updSourceId: await codeOf(tx, `UPDATE match_results SET source_id = '${s.srcB}'`),
      updXg: await codeOf(tx, `UPDATE match_stats SET home_xg = 9.999`),
      updShots: await codeOf(tx, `UPDATE match_stats SET home_shots = 99`),
      delResult: await codeOf(tx, `DELETE FROM match_results`),
      delStats: await codeOf(tx, `DELETE FROM match_stats`),
      truncResult: await codeOf(tx, `TRUNCATE match_results`),
      truncStats: await codeOf(tx, `TRUNCATE match_stats`),
    };
  });
  for (const k of ["insertResult", "insertStats", "supersedeResult", "supersedeStats"] as const) {
    check(`52. engine_rw PERMITTED: ${k}`, eng[k] === "none", eng[k]);
  }
  for (const k of [
    "updFtHome", "updFtAway", "updHtHome", "updPens", "updTrainable", "updResultSource",
    "updKnownAt", "updSourceId", "updXg", "updShots",
    "delResult", "delStats", "truncResult", "truncStats",
  ] as const) {
    check(`53. engine_rw REFUSED: ${k}`, eng[k] === "42501", eng[k]);
  }

  console.log("\n  grants — read-only roles:");
  for (const role of ["app_rw", "analytics_ro"]) {
    const ro = await inRollback(async (tx) => {
      const s = await seed(tx, `ro-${role}`);
      const f = await newFixture(tx, s, "ro");
      await tx.unsafe(res(s, f, "ft_home, ft_away", "1, 0"));
      await tx.unsafe(stats(s, f, "home_shots", "9"));
      await tx.unsafe(`SET LOCAL ROLE ${role}`);
      const [n] = await tx<{ n: number }[]>`
        SELECT (SELECT count(*)::int FROM match_results WHERE fixture_id = ${f})
             + (SELECT count(*)::int FROM match_stats   WHERE fixture_id = ${f}) AS n`;
      return {
        rows: n!.n,
        asOfR: await codeOf(tx, `SELECT 1 FROM match_results_as_of(now())`),
        asOfS: await codeOf(tx, `SELECT 1 FROM match_stats_as_of(now())`),
        insert: await codeOf(tx, res(s, f, "ft_home, ft_away", "5, 5", { source: s.srcB })),
        update: await codeOf(tx, `UPDATE match_results SET superseded_at = now()`),
        del: await codeOf(tx, `DELETE FROM match_stats`),
        trunc: await codeOf(tx, `TRUNCATE match_results`),
      };
    });
    check(`54. ${role} may SELECT both fact tables`, ro.rows === 2, `${ro.rows}`);
    check(`55. ${role} may call both as-of wrappers`, ro.asOfR === "none" && ro.asOfS === "none");
    check(`56. ${role} may NOT INSERT`, ro.insert === "42501", ro.insert);
    check(`57. ${role} may NOT UPDATE`, ro.update === "42501", ro.update);
    check(`58. ${role} may NOT DELETE`, ro.del === "42501", ro.del);
    check(`59. ${role} may NOT TRUNCATE`, ro.trunc === "42501", ro.trunc);
  }

  // =====================================================================
  console.log("\n  the as-of mechanism stays in exactly one place (§11.1):");
  const [pred] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n
      FROM pg_proc p JOIN pg_namespace n2 ON n2.oid = p.pronamespace
     WHERE n2.nspname = 'public' AND p.prokind = 'f'
       AND pg_get_functiondef(p.oid) LIKE ${`%${PREDICATE}%`}`;
  check(
    "60. catalog: still exactly ONE object defines the predicate, across FOUR wrappers",
    pred?.n === 1,
    `found ${pred?.n}`,
  );
  const wrappers = await sql<{ proname: string; prosrc: string; ret: string }[]>`
    SELECT proname, prosrc, pg_get_function_result(oid) AS ret FROM pg_proc
     WHERE proname IN ('match_results_as_of','match_stats_as_of') ORDER BY proname`;
  check("61. both P0-08 wrappers exist", wrappers.length === 2, wrappers.map((w) => w.proname).join(","));
  for (const w of wrappers) {
    check(
      `62. ${w.proname} delegates to fn_visible_at and never restates`,
      w.prosrc.includes("fn_visible_at") && !w.prosrc.includes(PREDICATE),
    );
    check(
      `63. ${w.proname} returns SETOF, preserving full column typing`,
      w.ret.startsWith("SETOF "),
      w.ret,
    );
  }
  const selfSrc = readFileSync(new URL(import.meta.url), "utf8");
  const restatements = selfSrc.split(PREDICATE).length - 1;
  check("64. this verification file restates the predicate ZERO times", restatements === 0, `${restatements}`);

  // =====================================================================
  console.log("\n  planning and the approved index set:");
  const plans = await inRollback(async (tx) => {
    const s = await seed(tx, "pl");
    await tx.unsafe(`
      INSERT INTO fixtures (season_id, stage, home_team_id, away_team_id)
      SELECT '${s.season}', 'bulk_' || g, '${s.home}', '${s.away}' FROM generate_series(1, 700) g`);
    await tx.unsafe(`
      INSERT INTO match_results (fixture_id, result_source, is_trainable, ft_home, ft_away,
                                 source_id, raw_payload_body_id, known_at, superseded_at)
      SELECT f.id, 'played', true, 1, 0, '${s.srcA}', ${s.body},
             timestamptz '2025-01-01Z' + (r * interval '30 days'),
             CASE WHEN r < 2 THEN timestamptz '2025-01-01Z' + ((r + 1) * interval '30 days') END
        FROM fixtures f CROSS JOIN generate_series(0, 2) r
       WHERE f.stage LIKE 'bulk|_%' ESCAPE '|'`);
    await tx.unsafe("ANALYZE match_results");
    const [f] = await tx<{ id: string }[]>`SELECT id FROM fixtures WHERE stage = 'bulk_1'`;
    const asOf = await planOf(
      tx,
      `SELECT id FROM match_results_as_of(timestamptz '2025-02-15Z') WHERE fixture_id = '${f!.id}'`,
    );
    const direct = await planOf(
      tx,
      `SELECT id FROM match_results WHERE fixture_id = '${f!.id}'
        AND fn_visible_at(known_at, superseded_at, timestamptz '2025-02-15Z')`,
    );
    const current = await planOf(
      tx,
      `SELECT id FROM match_results WHERE fixture_id = '${f!.id}' AND superseded_at IS NULL`,
    );
    const [n] = await tx<{ n: number }[]>`SELECT count(*)::int AS n FROM match_results`;
    return { asOf, direct, current, rows: n!.n };
  });
  check("65. the plan probe ran against a non-trivial table", plans.rows >= 2100, `${plans.rows} rows`);
  check("66. the as-of wrapper produces NO Function Scan — LANGUAGE sql is inlined",
    !plans.asOf.includes("Function Scan"), plans.asOf.split("\n")[0]);
  check("67. the wrapper's plan is IDENTICAL to calling fn_visible_at directly",
    plans.asOf === plans.direct);
  check("68. the FULL fixture_id index serves the as-of lookup — its whole purpose",
    plans.asOf.includes("match_results_fixture_idx"), plans.asOf.replace(/\n/g, " | "));
  check("69. the partial current index serves the settlement lookup",
    plans.current.includes("match_results_current_idx"), plans.current.replace(/\n/g, " | "));

  const idx = await sql<{ indexname: string }[]>`
    SELECT indexname FROM pg_indexes
     WHERE schemaname = 'public' AND tablename IN ('match_results','match_stats') ORDER BY indexname`;
  const names = idx.map((r) => r.indexname);
  const EXPECTED = [
    "match_results_current_idx", "match_results_fixture_idx", "match_results_pkey",
    "match_stats_current_idx", "match_stats_fixture_idx", "match_stats_pkey",
  ];
  check("70. exactly the approved index set exists — no more, no fewer",
    names.length === EXPECTED.length && EXPECTED.every((n) => names.includes(n)), names.join(","));
  check("71. no known_at index on either fact table",
    !names.some((n) => n.includes("known_at")), names.join(","));

  // =====================================================================
  console.log("\n  P0-09+ boundary — P0-08 built nothing beyond its scope:");
  const [beyond] = await sql<{ t: string | null }[]>`
    SELECT string_agg(tablename, ',') AS t FROM pg_tables
     WHERE schemaname = 'public'
       AND tablename IN ('match_events','odds_coverage','odds_poll_windows','market_consensus',
                         'value_signals','prediction_markets','prediction_outcomes',
                         'team_ratings','standings','fixture_match_candidates','competition_coverage')`;
  // bookmakers/odds_series/odds_ticks were on this list until P0-09 landed and
  // created them by approved design; `predictions` came off it when P1-01
  // built it, likewise by approved design (PREDICTIONS.md). What the check
  // still defends is real: odds_coverage is deferred not built (§14.9), value
  // signals belong to a value engine that does not exist, and there is
  // deliberately NO per-market prediction table - the scoreline matrix is the
  // artifact and prediction_markets/prediction_outcomes would let two rows
  // disagree about one match.
  check("72. none of the P0-10+ tables exist", beyond?.t === null, beyond?.t ?? "");
  const [trg] = await sql<{ n: number }[]>`
    SELECT count(*)::int AS n FROM pg_trigger
     WHERE NOT tgisinternal AND tgrelid IN ('match_results'::regclass, 'match_stats'::regclass)`;
  check("73. no triggers on either fact table", trg?.n === 0, `${trg?.n}`);
  const [ext] = await sql<{ def: string }[]>`
    SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint
     WHERE conname = 'external_ids_entity_type_check'`;
  check("74. P0-06 external_ids is untouched — no fixture/result entity type",
    !!ext?.def && !/fixture|result|stat/.test(ext.def), ext?.def ?? "absent");

  console.log(failures === 0 ? "\nall invariants hold" : `\n${failures} invariant(s) violated`);
  if (failures > 0) process.exitCode = 1;
} finally {
  await sql.end({ timeout: 5 });
}
