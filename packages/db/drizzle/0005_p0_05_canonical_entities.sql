CREATE TABLE "competition_names" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "competition_names_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"competition_id" uuid NOT NULL,
	"name" text NOT NULL,
	"name_type" text NOT NULL,
	"valid_from" date NOT NULL,
	"valid_to" date,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "competition_names_name_type_check" CHECK ("competition_names"."name_type" IN ('official','sponsored','short','abbreviation')),
	CONSTRAINT "competition_names_interval_check" CHECK ("competition_names"."valid_to" IS NULL OR "competition_names"."valid_to" > "competition_names"."valid_from")
);
--> statement-breakpoint
CREATE TABLE "competitions" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"slug" text NOT NULL,
	"country_id" uuid,
	"type" text NOT NULL,
	"tier" integer,
	"confederation" text,
	"gender" text NOT NULL,
	"age_group" text DEFAULT 'senior' NOT NULL,
	"is_reserve_competition" boolean DEFAULT false NOT NULL,
	"is_active" boolean DEFAULT true NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "competitions_slug_unique" UNIQUE("slug"),
	CONSTRAINT "competitions_type_check" CHECK ("competitions"."type" IN ('league','domestic_cup','super_cup','continental','international')),
	CONSTRAINT "competitions_gender_check" CHECK ("competitions"."gender" IN ('men','women')),
	CONSTRAINT "competitions_age_group_check" CHECK ("competitions"."age_group" IN ('senior','u23','u21','u20','u19','u18','u17')),
	CONSTRAINT "competitions_tier_check" CHECK ("competitions"."tier" IS NULL OR "competitions"."tier" >= 1),
	CONSTRAINT "competitions_confederation_check" CHECK ("competitions"."confederation" IS NULL OR "competitions"."confederation" IN ('uefa','conmebol','concacaf','caf','afc','ofc','fifa'))
);
--> statement-breakpoint
CREATE TABLE "countries" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"slug" text NOT NULL,
	"name" text NOT NULL,
	"iso_alpha2" char(2),
	"fifa_code" char(3),
	"confederation" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "countries_slug_unique" UNIQUE("slug"),
	CONSTRAINT "countries_confederation_check" CHECK ("countries"."confederation" IS NULL OR "countries"."confederation" IN ('uefa','conmebol','concacaf','caf','afc','ofc','fifa')),
	CONSTRAINT "countries_iso_alpha2_check" CHECK ("countries"."iso_alpha2" IS NULL OR "countries"."iso_alpha2" ~ '^[A-Z]{2}$'),
	CONSTRAINT "countries_fifa_code_check" CHECK ("countries"."fifa_code" IS NULL OR "countries"."fifa_code" ~ '^[A-Z]{3}$')
);
--> statement-breakpoint
CREATE TABLE "seasons" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"slug" text NOT NULL,
	"competition_id" uuid NOT NULL,
	"label" text NOT NULL,
	"start_year" integer NOT NULL,
	"start_date" date,
	"end_date" date,
	"is_current" boolean DEFAULT false NOT NULL,
	"format" jsonb DEFAULT '{}'::jsonb NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "seasons_slug_unique" UNIQUE("slug"),
	CONSTRAINT "seasons_competition_label_key" UNIQUE("competition_id","label"),
	CONSTRAINT "seasons_dates_check" CHECK ("seasons"."end_date" IS NULL OR "seasons"."start_date" IS NULL OR "seasons"."end_date" >= "seasons"."start_date"),
	CONSTRAINT "seasons_start_year_check" CHECK ("seasons"."start_year" BETWEEN 1850 AND 2200)
);
--> statement-breakpoint
CREATE TABLE "team_aliases" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "team_aliases_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"team_id" uuid NOT NULL,
	"alias" text NOT NULL,
	"normalized_alias" text NOT NULL,
	"source_id" uuid,
	"confidence" smallint DEFAULT 100 NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "team_aliases_confidence_check" CHECK ("team_aliases"."confidence" BETWEEN 0 AND 100),
	CONSTRAINT "team_aliases_normalized_lower_check" CHECK ("team_aliases"."normalized_alias" = lower("team_aliases"."normalized_alias")),
	CONSTRAINT "team_aliases_normalized_nonempty_check" CHECK (length("team_aliases"."normalized_alias") > 0)
);
--> statement-breakpoint
CREATE TABLE "team_names" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "team_names_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"team_id" uuid NOT NULL,
	"name" text NOT NULL,
	"name_type" text NOT NULL,
	"valid_from" date NOT NULL,
	"valid_to" date,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "team_names_name_type_check" CHECK ("team_names"."name_type" IN ('official','common','short','abbreviation')),
	CONSTRAINT "team_names_interval_check" CHECK ("team_names"."valid_to" IS NULL OR "team_names"."valid_to" > "team_names"."valid_from")
);
--> statement-breakpoint
CREATE TABLE "teams" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"slug" text NOT NULL,
	"country_id" uuid NOT NULL,
	"team_type" text DEFAULT 'club' NOT NULL,
	"gender" text NOT NULL,
	"age_group" text DEFAULT 'senior' NOT NULL,
	"is_reserve" boolean DEFAULT false NOT NULL,
	"parent_team_id" uuid,
	"status" text DEFAULT 'active' NOT NULL,
	"succeeded_by_team_id" uuid,
	"continuity" text,
	"founded_year" integer,
	"crest_url" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "teams_slug_unique" UNIQUE("slug"),
	CONSTRAINT "teams_team_type_check" CHECK ("teams"."team_type" IN ('club','national')),
	CONSTRAINT "teams_gender_check" CHECK ("teams"."gender" IN ('men','women')),
	CONSTRAINT "teams_age_group_check" CHECK ("teams"."age_group" IN ('senior','u23','u21','u20','u19','u18','u17')),
	CONSTRAINT "teams_status_check" CHECK ("teams"."status" IN ('active','dissolved','merged')),
	CONSTRAINT "teams_continuity_check" CHECK ("teams"."continuity" IS NULL OR "teams"."continuity" IN ('legal','sporting','none')),
	CONSTRAINT "teams_parent_not_self_check" CHECK ("teams"."parent_team_id" IS NULL OR "teams"."parent_team_id" <> "teams"."id"),
	CONSTRAINT "teams_successor_not_self_check" CHECK ("teams"."succeeded_by_team_id" IS NULL OR "teams"."succeeded_by_team_id" <> "teams"."id"),
	CONSTRAINT "teams_founded_year_check" CHECK ("teams"."founded_year" IS NULL OR "teams"."founded_year" BETWEEN 1800 AND 2200)
);
--> statement-breakpoint
CREATE TABLE "venues" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"slug" text NOT NULL,
	"name" text NOT NULL,
	"city" text,
	"country_id" uuid,
	"capacity" integer,
	"latitude" numeric(9, 6),
	"longitude" numeric(9, 6),
	"opened_year" integer,
	"closed_year" integer,
	"status" text DEFAULT 'active' NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "venues_slug_unique" UNIQUE("slug"),
	CONSTRAINT "venues_status_check" CHECK ("venues"."status" IN ('active','closed','demolished')),
	CONSTRAINT "venues_capacity_check" CHECK ("venues"."capacity" IS NULL OR "venues"."capacity" >= 0),
	CONSTRAINT "venues_closed_after_opened_check" CHECK ("venues"."closed_year" IS NULL OR "venues"."opened_year" IS NULL OR "venues"."closed_year" >= "venues"."opened_year")
);
--> statement-breakpoint
ALTER TABLE "competition_names" ADD CONSTRAINT "competition_names_competition_id_competitions_id_fk" FOREIGN KEY ("competition_id") REFERENCES "public"."competitions"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "competitions" ADD CONSTRAINT "competitions_country_id_countries_id_fk" FOREIGN KEY ("country_id") REFERENCES "public"."countries"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "seasons" ADD CONSTRAINT "seasons_competition_id_competitions_id_fk" FOREIGN KEY ("competition_id") REFERENCES "public"."competitions"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "team_aliases" ADD CONSTRAINT "team_aliases_team_id_teams_id_fk" FOREIGN KEY ("team_id") REFERENCES "public"."teams"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "team_aliases" ADD CONSTRAINT "team_aliases_source_id_data_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."data_sources"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "team_names" ADD CONSTRAINT "team_names_team_id_teams_id_fk" FOREIGN KEY ("team_id") REFERENCES "public"."teams"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "teams" ADD CONSTRAINT "teams_country_id_countries_id_fk" FOREIGN KEY ("country_id") REFERENCES "public"."countries"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "teams" ADD CONSTRAINT "teams_parent_team_id_teams_id_fk" FOREIGN KEY ("parent_team_id") REFERENCES "public"."teams"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "teams" ADD CONSTRAINT "teams_succeeded_by_team_id_teams_id_fk" FOREIGN KEY ("succeeded_by_team_id") REFERENCES "public"."teams"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "venues" ADD CONSTRAINT "venues_country_id_countries_id_fk" FOREIGN KEY ("country_id") REFERENCES "public"."countries"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE UNIQUE INDEX "competition_names_current_idx" ON "competition_names" USING btree ("competition_id","name_type") WHERE "competition_names"."valid_to" IS NULL;--> statement-breakpoint
CREATE UNIQUE INDEX "seasons_one_current_idx" ON "seasons" USING btree ("competition_id") WHERE "seasons"."is_current";--> statement-breakpoint
CREATE UNIQUE INDEX "team_aliases_global_idx" ON "team_aliases" USING btree ("normalized_alias") WHERE "team_aliases"."source_id" IS NULL;--> statement-breakpoint
CREATE UNIQUE INDEX "team_aliases_source_idx" ON "team_aliases" USING btree ("source_id","normalized_alias") WHERE "team_aliases"."source_id" IS NOT NULL;--> statement-breakpoint
CREATE UNIQUE INDEX "team_names_current_idx" ON "team_names" USING btree ("team_id","name_type") WHERE "team_names"."valid_to" IS NULL;