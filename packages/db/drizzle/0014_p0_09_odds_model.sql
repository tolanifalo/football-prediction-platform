CREATE TABLE "bookmakers" (
	"id" uuid PRIMARY KEY DEFAULT gen_random_uuid() NOT NULL,
	"slug" text NOT NULL,
	"name" text NOT NULL,
	"kind" text NOT NULL,
	"country_scope" text,
	"commission_rate" numeric(5, 4),
	"sharpness_tier" text,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "bookmakers_slug_unique" UNIQUE("slug"),
	CONSTRAINT "bookmakers_kind_check" CHECK ("bookmakers"."kind" IN ('bookmaker','exchange')),
	CONSTRAINT "bookmakers_commission_only_exchange_check" CHECK ("bookmakers"."commission_rate" IS NULL OR "bookmakers"."kind" = 'exchange'),
	CONSTRAINT "bookmakers_commission_range_check" CHECK ("bookmakers"."commission_rate" IS NULL OR ("bookmakers"."commission_rate" >= 0 AND "bookmakers"."commission_rate" < 1))
);
--> statement-breakpoint
CREATE TABLE "odds_series" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "odds_series_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"fixture_id" uuid NOT NULL,
	"bookmaker_id" uuid NOT NULL,
	"period" text NOT NULL,
	"market_type" text NOT NULL,
	"line" numeric(6, 2),
	"selection" text NOT NULL,
	"side" text DEFAULT 'back' NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	CONSTRAINT "odds_series_key" UNIQUE NULLS NOT DISTINCT("fixture_id","bookmaker_id","period","market_type","line","selection","side"),
	CONSTRAINT "odds_series_period_check" CHECK ("odds_series"."period" IN ('ft','ht','2h')),
	CONSTRAINT "odds_series_side_check" CHECK ("odds_series"."side" IN ('back','lay')),
	CONSTRAINT "odds_series_market_type_check" CHECK ("odds_series"."market_type" IN ('1x2','over_under','btts','asian_handicap')),
	CONSTRAINT "odds_series_line_presence_check" CHECK (("odds_series"."market_type" IN ('over_under','asian_handicap')) = ("odds_series"."line" IS NOT NULL))
);
--> statement-breakpoint
CREATE TABLE "odds_ticks" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "odds_ticks_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"series_id" bigint NOT NULL,
	"source_id" uuid NOT NULL,
	"raw_payload_body_id" bigint NOT NULL,
	"price" numeric(9, 4),
	"is_available" boolean NOT NULL,
	"price_kind" text DEFAULT 'observed' NOT NULL,
	"observed_at" timestamp with time zone NOT NULL,
	"provider_at" timestamp with time zone,
	"known_at" timestamp with time zone DEFAULT now() NOT NULL,
	"superseded_at" timestamp with time zone,
	CONSTRAINT "odds_ticks_price_kind_check" CHECK ("odds_ticks"."price_kind" IN ('observed','provider_opening','provider_closing','exchange_sp')),
	CONSTRAINT "odds_ticks_price_range_check" CHECK ("odds_ticks"."price" IS NULL OR ("odds_ticks"."price" >= 1.01 AND "odds_ticks"."price" <= 100000)),
	CONSTRAINT "odds_ticks_unavailable_has_no_price_check" CHECK ("odds_ticks"."is_available" OR "odds_ticks"."price" IS NULL),
	CONSTRAINT "odds_ticks_superseded_after_known_check" CHECK ("odds_ticks"."superseded_at" IS NULL OR "odds_ticks"."superseded_at" > "odds_ticks"."known_at")
);
--> statement-breakpoint
ALTER TABLE "odds_series" ADD CONSTRAINT "odds_series_fixture_id_fixtures_id_fk" FOREIGN KEY ("fixture_id") REFERENCES "public"."fixtures"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "odds_series" ADD CONSTRAINT "odds_series_bookmaker_id_bookmakers_id_fk" FOREIGN KEY ("bookmaker_id") REFERENCES "public"."bookmakers"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "odds_ticks" ADD CONSTRAINT "odds_ticks_series_id_odds_series_id_fk" FOREIGN KEY ("series_id") REFERENCES "public"."odds_series"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "odds_ticks" ADD CONSTRAINT "odds_ticks_source_id_data_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."data_sources"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "odds_ticks" ADD CONSTRAINT "odds_ticks_raw_payload_body_id_raw_payload_bodies_id_fk" FOREIGN KEY ("raw_payload_body_id") REFERENCES "public"."raw_payload_bodies"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "odds_series_fixture_idx" ON "odds_series" USING btree ("fixture_id");--> statement-breakpoint
CREATE INDEX "odds_series_market_idx" ON "odds_series" USING btree ("fixture_id","period","market_type","line","selection");--> statement-breakpoint
CREATE UNIQUE INDEX "odds_ticks_current_idx" ON "odds_ticks" USING btree ("series_id","source_id","observed_at","price_kind") WHERE "odds_ticks"."superseded_at" IS NULL;--> statement-breakpoint
CREATE INDEX "odds_ticks_series_time_idx" ON "odds_ticks" USING btree ("series_id","observed_at" DESC NULLS LAST);