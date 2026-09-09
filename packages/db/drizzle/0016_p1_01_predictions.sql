CREATE TABLE "predictions" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "predictions_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"fixture_id" uuid NOT NULL,
	"model_family" text NOT NULL,
	"model_version" text NOT NULL,
	"profile" text NOT NULL,
	"data_cutoff" timestamp with time zone NOT NULL,
	"known_at" timestamp with time zone DEFAULT now() NOT NULL,
	"superseded_at" timestamp with time zone,
	"lambda_home" double precision NOT NULL,
	"lambda_away" double precision NOT NULL,
	"max_goals" smallint NOT NULL,
	"scoreline" double precision[] NOT NULL,
	"truncated_mass" double precision NOT NULL,
	"p_home" double precision NOT NULL,
	"p_draw" double precision NOT NULL,
	"p_away" double precision NOT NULL,
	"p_over_0_5" double precision NOT NULL,
	"p_over_1_5" double precision NOT NULL,
	"p_over_2_5" double precision NOT NULL,
	"p_over_3_5" double precision NOT NULL,
	"p_btts_yes" double precision NOT NULL,
	"is_cold_start" boolean NOT NULL,
	"cold_started_teams" text[] DEFAULT '{}' NOT NULL,
	"training_matches" integer NOT NULL,
	"fit_metadata" jsonb NOT NULL,
	"job_run_id" bigint,
	CONSTRAINT "predictions_cutoff_not_after_known_check" CHECK ("predictions"."data_cutoff" <= "predictions"."known_at"),
	CONSTRAINT "predictions_superseded_after_known_check" CHECK ("predictions"."superseded_at" IS NULL OR "predictions"."superseded_at" > "predictions"."known_at"),
	CONSTRAINT "predictions_lambda_positive_check" CHECK ("predictions"."lambda_home" > 0 AND "predictions"."lambda_away" > 0),
	CONSTRAINT "predictions_max_goals_check" CHECK ("predictions"."max_goals" BETWEEN 0 AND 100),
	CONSTRAINT "predictions_scoreline_shape_check" CHECK (cardinality("predictions"."scoreline") = ("predictions"."max_goals" + 1) * ("predictions"."max_goals" + 1)),
	CONSTRAINT "predictions_truncated_mass_check" CHECK ("predictions"."truncated_mass" >= 0 AND "predictions"."truncated_mass" < 1),
	CONSTRAINT "predictions_probabilities_bounded_check" CHECK ("predictions"."p_home" BETWEEN 0 AND 1 AND "predictions"."p_draw" BETWEEN 0 AND 1
          AND "predictions"."p_away" BETWEEN 0 AND 1 AND "predictions"."p_btts_yes" BETWEEN 0 AND 1
          AND "predictions"."p_over_0_5" BETWEEN 0 AND 1 AND "predictions"."p_over_1_5" BETWEEN 0 AND 1
          AND "predictions"."p_over_2_5" BETWEEN 0 AND 1 AND "predictions"."p_over_3_5" BETWEEN 0 AND 1),
	CONSTRAINT "predictions_one_x_two_sums_check" CHECK (abs(("predictions"."p_home" + "predictions"."p_draw" + "predictions"."p_away") - 1) < 1e-9),
	CONSTRAINT "predictions_over_monotonic_check" CHECK ("predictions"."p_over_0_5" >= "predictions"."p_over_1_5"
          AND "predictions"."p_over_1_5" >= "predictions"."p_over_2_5"
          AND "predictions"."p_over_2_5" >= "predictions"."p_over_3_5"),
	CONSTRAINT "predictions_training_matches_check" CHECK ("predictions"."training_matches" >= 0),
	CONSTRAINT "predictions_cold_start_consistent_check" CHECK ("predictions"."is_cold_start" = (cardinality("predictions"."cold_started_teams") > 0))
);
--> statement-breakpoint
ALTER TABLE "predictions" ADD CONSTRAINT "predictions_fixture_id_fixtures_id_fk" FOREIGN KEY ("fixture_id") REFERENCES "public"."fixtures"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "predictions" ADD CONSTRAINT "predictions_job_run_id_job_runs_id_fk" FOREIGN KEY ("job_run_id") REFERENCES "public"."job_runs"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "predictions_current_idx" ON "predictions" USING btree ("fixture_id","model_version","profile","data_cutoff") WHERE "predictions"."superseded_at" IS NULL;--> statement-breakpoint
CREATE INDEX "predictions_fixture_idx" ON "predictions" USING btree ("fixture_id") WHERE "predictions"."superseded_at" IS NULL;--> statement-breakpoint
CREATE INDEX "predictions_job_run_idx" ON "predictions" USING btree ("job_run_id");