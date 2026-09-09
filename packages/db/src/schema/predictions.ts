import { sql } from "drizzle-orm";
import {
  bigint,
  boolean,
  check,
  doublePrecision,
  index,
  integer,
  jsonb,
  pgTable,
  smallint,
  text,
  timestamp,
  uniqueIndex,
  uuid,
} from "drizzle-orm/pg-core";

import { fixtures } from "./fixtures.ts";
import { jobRuns } from "./job-runs.ts";

/**
 * Model predictions, persisted for the web read path (PREDICTIONS.md).
 *
 * NOT A PROVIDER FACT, and deliberately shaped differently from one. §6 rule 6
 * defines a fact as "a claim two providers could disagree about" and requires
 * source_id + raw_payload_body_id on every one. A prediction is OUR derived
 * claim about a match nobody has observed yet; no provider supplied it and no
 * archived bytes contain it. Its provenance anchor is instead the pair that
 * makes it reproducible: WHICH MODEL, and WHAT IT WAS ALLOWED TO KNOW.
 *
 * TWO TIMESTAMPS, AND THEY ARE NOT THE SAME AXIS (§2.1):
 *
 *   data_cutoff  what football information the model was permitted to use.
 *                Training is strictly before it. This is VALID time and it is
 *                part of the identity.
 *   known_at     when we generated and recorded the prediction. TRANSACTION
 *                time, and NOT part of the identity - regenerating the same
 *                prediction tomorrow is the same prediction.
 *
 * `data_cutoff <= known_at` is a CHECK because the converse is incoherent: a
 * prediction recorded at T cannot have been entitled to football knowledge
 * from after T.
 *
 * IDENTITY IS (fixture, model_version, profile, data_cutoff). Rerunning with
 * all four the same must not create a second artifact, so the writer compares
 * content and writes nothing when it matches. When it does NOT match - because
 * an underlying result was revised beneath the same cutoff - the old row is
 * superseded and a new one inserted, exactly as fixture_schedule does. There
 * is no "latest" column and nothing is ever overwritten in place: a historical
 * prediction stays readable at its own cutoff forever.
 *
 * ONE TABLE, NOT ONE PER MARKET. The scoreline matrix is the artifact; 1X2,
 * totals and BTTS are read off it. Separate market tables would let two rows
 * disagree about the same match, which is precisely the failure the
 * single-matrix rule exists to prevent (§6 rule 10).
 *
 * DOUBLE PRECISION, NEVER NUMERIC. The model computes in float64 and `numeric`
 * would force a scale - that is rounding, chosen silently, applied to every
 * probability on the way in. float8 stores exactly what was computed and reads
 * back bit-identical, which is what makes "the persisted artifact equals the
 * direct model output" a testable claim rather than an approximate one.
 *
 * NO ODDS, NO VALUE, NO RECOMMENDATIONS. The model/odds wall is rule 1; a
 * prices column here would put bookmaker data one join from the estimator.
 */
export const predictions = pgTable(
  "predictions",
  {
    id: bigint("id", { mode: "bigint" }).generatedAlwaysAsIdentity().primaryKey(),
    fixtureId: uuid("fixture_id").notNull().references(() => fixtures.id),

    /** Model identity — the half of reproducibility that is not the cutoff. */
    modelFamily: text("model_family").notNull(),
    modelVersion: text("model_version").notNull(),
    profile: text("profile").notNull(),

    /** Valid time: what the model was allowed to know. Part of the identity. */
    dataCutoff: timestamp("data_cutoff", { withTimezone: true }).notNull(),
    /** Transaction time: when we produced it. NOT part of the identity. */
    knownAt: timestamp("known_at", { withTimezone: true }).notNull().defaultNow(),
    supersededAt: timestamp("superseded_at", { withTimezone: true }),

    lambdaHome: doublePrecision("lambda_home").notNull(),
    lambdaAway: doublePrecision("lambda_away").notNull(),

    /**
     * The joint distribution, ROW-MAJOR AND FLAT: index `h * (max_goals+1) + a`
     * is P(home h, away a). Flat rather than a 2-D array because the shape is
     * then a single cardinality CHECK against max_goals, and no driver has to
     * agree with us about multidimensional array bounds.
     */
    maxGoals: smallint("max_goals").notNull(),
    scoreline: doublePrecision("scoreline").array().notNull(),
    /** Mass outside the grid BEFORE renormalisation. Reported, never hidden. */
    truncatedMass: doublePrecision("truncated_mass").notNull(),

    /** 1X2 stored complete: it is a three-way market and displayed as three. */
    pHome: doublePrecision("p_home").notNull(),
    pDraw: doublePrecision("p_draw").notNull(),
    pAway: doublePrecision("p_away").notNull(),

    /**
     * Two-way markets store ONE side. `under = 1 - over` and `btts_no =
     * 1 - btts_yes` are exact, so a second column would be a redundant copy
     * that can drift out of step with the first.
     */
    pOver05: doublePrecision("p_over_0_5").notNull(),
    pOver15: doublePrecision("p_over_1_5").notNull(),
    pOver25: doublePrecision("p_over_2_5").notNull(),
    pOver35: doublePrecision("p_over_3_5").notNull(),
    pBttsYes: doublePrecision("p_btts_yes").notNull(),

    /** Cold start is never silent: the flag AND which sides caused it. */
    isColdStart: boolean("is_cold_start").notNull(),
    coldStartedTeams: text("cold_started_teams").array().notNull().default(sql`'{}'`),

    /** Enough to reproduce the fit: counts here, configuration in the jsonb. */
    trainingMatches: integer("training_matches").notNull(),
    fitMetadata: jsonb("fit_metadata").notNull(),

    jobRunId: bigint("job_run_id", { mode: "bigint" }).references(() => jobRuns.id),
  },
  (t) => [
    // Exactly one CURRENT prediction per (fixture, model, cutoff). A rerun
    // that changes nothing writes nothing; one that changes something
    // supersedes first, so this never blocks a legitimate revision.
    uniqueIndex("predictions_current_idx")
      .on(t.fixtureId, t.modelVersion, t.profile, t.dataCutoff)
      .where(sql`${t.supersededAt} IS NULL`),
    // The web read path: current predictions for one fixture.
    index("predictions_fixture_idx").on(t.fixtureId).where(sql`${t.supersededAt} IS NULL`),
    // Operational: everything one job run produced.
    index("predictions_job_run_idx").on(t.jobRunId),

    check(
      "predictions_cutoff_not_after_known_check",
      sql`${t.dataCutoff} <= ${t.knownAt}`,
    ),
    check(
      "predictions_superseded_after_known_check",
      sql`${t.supersededAt} IS NULL OR ${t.supersededAt} > ${t.knownAt}`,
    ),
    check(
      "predictions_lambda_positive_check",
      sql`${t.lambdaHome} > 0 AND ${t.lambdaAway} > 0`,
    ),
    check("predictions_max_goals_check", sql`${t.maxGoals} BETWEEN 0 AND 100`),
    // The stored shape must match the declared one, or the matrix is
    // uninterpretable and every derived market read from it is wrong.
    check(
      "predictions_scoreline_shape_check",
      sql`cardinality(${t.scoreline}) = (${t.maxGoals} + 1) * (${t.maxGoals} + 1)`,
    ),
    check(
      "predictions_truncated_mass_check",
      sql`${t.truncatedMass} >= 0 AND ${t.truncatedMass} < 1`,
    ),
    // Every probability is a probability.
    check(
      "predictions_probabilities_bounded_check",
      sql`${t.pHome} BETWEEN 0 AND 1 AND ${t.pDraw} BETWEEN 0 AND 1
          AND ${t.pAway} BETWEEN 0 AND 1 AND ${t.pBttsYes} BETWEEN 0 AND 1
          AND ${t.pOver05} BETWEEN 0 AND 1 AND ${t.pOver15} BETWEEN 0 AND 1
          AND ${t.pOver25} BETWEEN 0 AND 1 AND ${t.pOver35} BETWEEN 0 AND 1`,
    ),
    // 1X2 is exhaustive and mutually exclusive. The tolerance is float64
    // summation slack over ~256 cells, not a licence to store a bad triple.
    check(
      "predictions_one_x_two_sums_check",
      sql`abs((${t.pHome} + ${t.pDraw} + ${t.pAway}) - 1) < 1e-9`,
    ),
    // More goals cannot be more likely than fewer.
    check(
      "predictions_over_monotonic_check",
      sql`${t.pOver05} >= ${t.pOver15}
          AND ${t.pOver15} >= ${t.pOver25}
          AND ${t.pOver25} >= ${t.pOver35}`,
    ),
    check("predictions_training_matches_check", sql`${t.trainingMatches} >= 0`),
    // A cold-started prediction must say which side caused it, and a
    // prediction naming a cold side must not claim it is warm.
    check(
      "predictions_cold_start_consistent_check",
      sql`${t.isColdStart} = (cardinality(${t.coldStartedTeams}) > 0)`,
    ),
  ],
);
