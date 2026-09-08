CREATE TABLE "fixture_schedule" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "fixture_schedule_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"fixture_id" uuid NOT NULL,
	"kickoff_utc" timestamp with time zone NOT NULL,
	"local_date" date NOT NULL,
	"local_tz" text NOT NULL,
	"venue_id" uuid,
	"status" text NOT NULL,
	"is_neutral_venue" boolean DEFAULT false NOT NULL,
	"source_id" uuid NOT NULL,
	"raw_payload_body_id" bigint NOT NULL,
	"known_at" timestamp with time zone DEFAULT now() NOT NULL,
	"superseded_at" timestamp with time zone,
	CONSTRAINT "fixture_schedule_status_check" CHECK ("fixture_schedule"."status" IN ('scheduled','live','suspended','ft','postponed','abandoned','cancelled')),
	CONSTRAINT "fixture_schedule_superseded_after_known_check" CHECK ("fixture_schedule"."superseded_at" IS NULL OR "fixture_schedule"."superseded_at" > "fixture_schedule"."known_at"),
	CONSTRAINT "fixture_schedule_local_date_check" CHECK ("fixture_schedule"."local_date" = ("fixture_schedule"."kickoff_utc" AT TIME ZONE "fixture_schedule"."local_tz")::date)
);
--> statement-breakpoint
CREATE TABLE "fixtures" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"season_id" uuid NOT NULL,
	"stage" text NOT NULL,
	"leg" integer DEFAULT 1 NOT NULL,
	"replay_number" integer DEFAULT 0 NOT NULL,
	"home_team_id" uuid NOT NULL,
	"away_team_id" uuid NOT NULL,
	"tie_id" uuid,
	"replaces_fixture_id" uuid,
	"stats_complete_at" timestamp with time zone,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "fixtures_identity_key" UNIQUE("season_id","stage","leg","replay_number","home_team_id","away_team_id"),
	CONSTRAINT "fixtures_teams_differ_check" CHECK ("fixtures"."home_team_id" <> "fixtures"."away_team_id"),
	CONSTRAINT "fixtures_leg_check" CHECK ("fixtures"."leg" IN (1,2)),
	CONSTRAINT "fixtures_replay_number_check" CHECK ("fixtures"."replay_number" >= 0),
	CONSTRAINT "fixtures_replaces_not_self_check" CHECK ("fixtures"."replaces_fixture_id" IS NULL OR "fixtures"."replaces_fixture_id" <> "fixtures"."id"),
	CONSTRAINT "fixtures_tie_leg_check" CHECK ("fixtures"."tie_id" IS NULL OR "fixtures"."leg" IN (1,2))
);
--> statement-breakpoint
ALTER TABLE "fixture_schedule" ADD CONSTRAINT "fixture_schedule_fixture_id_fixtures_id_fk" FOREIGN KEY ("fixture_id") REFERENCES "public"."fixtures"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fixture_schedule" ADD CONSTRAINT "fixture_schedule_venue_id_venues_id_fk" FOREIGN KEY ("venue_id") REFERENCES "public"."venues"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fixture_schedule" ADD CONSTRAINT "fixture_schedule_source_id_data_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."data_sources"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fixture_schedule" ADD CONSTRAINT "fixture_schedule_raw_payload_body_id_raw_payload_bodies_id_fk" FOREIGN KEY ("raw_payload_body_id") REFERENCES "public"."raw_payload_bodies"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fixtures" ADD CONSTRAINT "fixtures_season_id_seasons_id_fk" FOREIGN KEY ("season_id") REFERENCES "public"."seasons"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fixtures" ADD CONSTRAINT "fixtures_home_team_id_teams_id_fk" FOREIGN KEY ("home_team_id") REFERENCES "public"."teams"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fixtures" ADD CONSTRAINT "fixtures_away_team_id_teams_id_fk" FOREIGN KEY ("away_team_id") REFERENCES "public"."teams"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "fixtures" ADD CONSTRAINT "fixtures_replaces_fixture_id_fixtures_id_fk" FOREIGN KEY ("replaces_fixture_id") REFERENCES "public"."fixtures"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "fixture_schedule_current_idx" ON "fixture_schedule" USING btree ("fixture_id") WHERE "fixture_schedule"."superseded_at" IS NULL;--> statement-breakpoint
CREATE INDEX "fixture_schedule_kickoff_idx" ON "fixture_schedule" USING btree ("kickoff_utc") WHERE "fixture_schedule"."superseded_at" IS NULL;--> statement-breakpoint
CREATE INDEX "fixtures_home_team_idx" ON "fixtures" USING btree ("home_team_id");--> statement-breakpoint
CREATE INDEX "fixtures_away_team_idx" ON "fixtures" USING btree ("away_team_id");