CREATE TABLE "match_results" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "match_results_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"fixture_id" uuid NOT NULL,
	"result_source" text NOT NULL,
	"is_trainable" boolean NOT NULL,
	"ht_home" smallint,
	"ht_away" smallint,
	"ft_home" smallint NOT NULL,
	"ft_away" smallint NOT NULL,
	"aet_home" smallint,
	"aet_away" smallint,
	"pens_home" smallint,
	"pens_away" smallint,
	"occurred_at" timestamp with time zone,
	"source_id" uuid NOT NULL,
	"raw_payload_body_id" bigint NOT NULL,
	"known_at" timestamp with time zone DEFAULT now() NOT NULL,
	"superseded_at" timestamp with time zone,
	CONSTRAINT "match_results_source_check" CHECK ("match_results"."result_source" IN ('played','awarded')),
	CONSTRAINT "match_results_awarded_untrainable_check" CHECK ("match_results"."result_source" <> 'awarded' OR "match_results"."is_trainable" = false),
	CONSTRAINT "match_results_ft_nonneg_check" CHECK ("match_results"."ft_home" >= 0 AND "match_results"."ft_away" >= 0),
	CONSTRAINT "match_results_ht_pair_check" CHECK (("match_results"."ht_home" IS NULL) = ("match_results"."ht_away" IS NULL)),
	CONSTRAINT "match_results_ht_le_ft_check" CHECK ("match_results"."ht_home" IS NULL OR ("match_results"."ht_home" <= "match_results"."ft_home" AND "match_results"."ht_away" <= "match_results"."ft_away")),
	CONSTRAINT "match_results_aet_pair_check" CHECK (("match_results"."aet_home" IS NULL) = ("match_results"."aet_away" IS NULL)),
	CONSTRAINT "match_results_aet_ge_ft_check" CHECK ("match_results"."aet_home" IS NULL OR ("match_results"."aet_home" >= "match_results"."ft_home" AND "match_results"."aet_away" >= "match_results"."ft_away")),
	CONSTRAINT "match_results_pens_pair_check" CHECK (("match_results"."pens_home" IS NULL) = ("match_results"."pens_away" IS NULL)),
	CONSTRAINT "match_results_pens_need_aet_check" CHECK ("match_results"."pens_home" IS NULL OR "match_results"."aet_home" IS NOT NULL),
	CONSTRAINT "match_results_pens_decisive_check" CHECK ("match_results"."pens_home" IS NULL OR "match_results"."pens_home" <> "match_results"."pens_away"),
	CONSTRAINT "match_results_superseded_after_known_check" CHECK ("match_results"."superseded_at" IS NULL OR "match_results"."superseded_at" > "match_results"."known_at")
);
--> statement-breakpoint
CREATE TABLE "match_stats" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "match_stats_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"fixture_id" uuid NOT NULL,
	"home_shots" smallint,
	"away_shots" smallint,
	"home_shots_on_target" smallint,
	"away_shots_on_target" smallint,
	"home_corners" smallint,
	"away_corners" smallint,
	"home_fouls" smallint,
	"away_fouls" smallint,
	"home_yellow_cards" smallint,
	"away_yellow_cards" smallint,
	"home_red_cards" smallint,
	"away_red_cards" smallint,
	"home_possession" numeric(5, 2),
	"away_possession" numeric(5, 2),
	"home_xg" numeric(6, 3),
	"away_xg" numeric(6, 3),
	"source_id" uuid NOT NULL,
	"raw_payload_body_id" bigint NOT NULL,
	"known_at" timestamp with time zone DEFAULT now() NOT NULL,
	"superseded_at" timestamp with time zone,
	CONSTRAINT "match_stats_possession_pair_check" CHECK (("match_stats"."home_possession" IS NULL AND "match_stats"."away_possession" IS NULL)
          OR ("match_stats"."home_possession" + "match_stats"."away_possession" BETWEEN 99 AND 101)),
	CONSTRAINT "match_stats_home_sot_le_shots_check" CHECK ("match_stats"."home_shots_on_target" IS NULL OR "match_stats"."home_shots" IS NULL
          OR "match_stats"."home_shots_on_target" <= "match_stats"."home_shots"),
	CONSTRAINT "match_stats_away_sot_le_shots_check" CHECK ("match_stats"."away_shots_on_target" IS NULL OR "match_stats"."away_shots" IS NULL
          OR "match_stats"."away_shots_on_target" <= "match_stats"."away_shots"),
	CONSTRAINT "match_stats_counts_nonneg_check" CHECK (("match_stats"."home_shots" IS NULL OR "match_stats"."home_shots" >= 0)
          AND ("match_stats"."away_shots" IS NULL OR "match_stats"."away_shots" >= 0)
          AND ("match_stats"."home_corners" IS NULL OR "match_stats"."home_corners" >= 0)
          AND ("match_stats"."away_corners" IS NULL OR "match_stats"."away_corners" >= 0)
          AND ("match_stats"."home_fouls" IS NULL OR "match_stats"."home_fouls" >= 0)
          AND ("match_stats"."away_fouls" IS NULL OR "match_stats"."away_fouls" >= 0)
          AND ("match_stats"."home_yellow_cards" IS NULL OR "match_stats"."home_yellow_cards" >= 0)
          AND ("match_stats"."away_yellow_cards" IS NULL OR "match_stats"."away_yellow_cards" >= 0)
          AND ("match_stats"."home_red_cards" IS NULL OR "match_stats"."home_red_cards" >= 0)
          AND ("match_stats"."away_red_cards" IS NULL OR "match_stats"."away_red_cards" >= 0)),
	CONSTRAINT "match_stats_xg_nonneg_check" CHECK (("match_stats"."home_xg" IS NULL OR "match_stats"."home_xg" >= 0) AND ("match_stats"."away_xg" IS NULL OR "match_stats"."away_xg" >= 0)),
	CONSTRAINT "match_stats_superseded_after_known_check" CHECK ("match_stats"."superseded_at" IS NULL OR "match_stats"."superseded_at" > "match_stats"."known_at")
);
--> statement-breakpoint
ALTER TABLE "match_results" ADD CONSTRAINT "match_results_fixture_id_fixtures_id_fk" FOREIGN KEY ("fixture_id") REFERENCES "public"."fixtures"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "match_results" ADD CONSTRAINT "match_results_source_id_data_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."data_sources"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "match_results" ADD CONSTRAINT "match_results_raw_payload_body_id_raw_payload_bodies_id_fk" FOREIGN KEY ("raw_payload_body_id") REFERENCES "public"."raw_payload_bodies"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "match_stats" ADD CONSTRAINT "match_stats_fixture_id_fixtures_id_fk" FOREIGN KEY ("fixture_id") REFERENCES "public"."fixtures"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "match_stats" ADD CONSTRAINT "match_stats_source_id_data_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."data_sources"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "match_stats" ADD CONSTRAINT "match_stats_raw_payload_body_id_raw_payload_bodies_id_fk" FOREIGN KEY ("raw_payload_body_id") REFERENCES "public"."raw_payload_bodies"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "match_results_current_idx" ON "match_results" USING btree ("fixture_id","source_id") WHERE "match_results"."superseded_at" IS NULL;--> statement-breakpoint
CREATE INDEX "match_results_fixture_idx" ON "match_results" USING btree ("fixture_id");--> statement-breakpoint
CREATE UNIQUE INDEX "match_stats_current_idx" ON "match_stats" USING btree ("fixture_id","source_id") WHERE "match_stats"."superseded_at" IS NULL;--> statement-breakpoint
CREATE INDEX "match_stats_fixture_idx" ON "match_stats" USING btree ("fixture_id");