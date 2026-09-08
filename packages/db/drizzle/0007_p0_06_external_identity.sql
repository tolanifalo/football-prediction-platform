CREATE TABLE "entity_review_queue" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "entity_review_queue_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"entity_type" text NOT NULL,
	"source_id" uuid NOT NULL,
	"external_id" text NOT NULL,
	"candidate_internal_id" uuid,
	"reason" text NOT NULL,
	"status" text DEFAULT 'open' NOT NULL,
	"created_at" timestamp with time zone DEFAULT now() NOT NULL,
	"resolved_at" timestamp with time zone,
	"resolved_by" text,
	"resolution_note" text,
	CONSTRAINT "entity_review_queue_entity_type_check" CHECK ("entity_review_queue"."entity_type" IN ('country','competition','season','team','venue')),
	CONSTRAINT "entity_review_queue_status_check" CHECK ("entity_review_queue"."status" IN ('open','resolved','rejected')),
	CONSTRAINT "entity_review_queue_reason_check" CHECK (length("entity_review_queue"."reason") > 0),
	CONSTRAINT "entity_review_queue_resolution_check" CHECK (("entity_review_queue"."status" = 'open' AND "entity_review_queue"."resolved_at" IS NULL)
          OR ("entity_review_queue"."status" <> 'open' AND "entity_review_queue"."resolved_at" IS NOT NULL))
);
--> statement-breakpoint
CREATE TABLE "external_ids" (
	"id" bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY (sequence name "external_ids_id_seq" INCREMENT BY 1 MINVALUE 1 MAXVALUE 9223372036854775807 START WITH 1 CACHE 1),
	"source_id" uuid NOT NULL,
	"entity_type" text NOT NULL,
	"external_id" text NOT NULL,
	"internal_id" uuid NOT NULL,
	"confidence" smallint,
	"last_verified_at" timestamp with time zone,
	"known_at" timestamp with time zone DEFAULT now() NOT NULL,
	"superseded_at" timestamp with time zone,
	CONSTRAINT "external_ids_entity_type_check" CHECK ("external_ids"."entity_type" IN ('country','competition','season','team','venue')),
	CONSTRAINT "external_ids_superseded_after_known_check" CHECK ("external_ids"."superseded_at" IS NULL OR "external_ids"."superseded_at" > "external_ids"."known_at"),
	CONSTRAINT "external_ids_confidence_check" CHECK ("external_ids"."confidence" IS NULL OR "external_ids"."confidence" BETWEEN 0 AND 100)
);
--> statement-breakpoint
ALTER TABLE "entity_review_queue" ADD CONSTRAINT "entity_review_queue_source_id_data_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."data_sources"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
ALTER TABLE "external_ids" ADD CONSTRAINT "external_ids_source_id_data_sources_id_fk" FOREIGN KEY ("source_id") REFERENCES "public"."data_sources"("id") ON DELETE no action ON UPDATE no action;--> statement-breakpoint
CREATE INDEX "entity_review_queue_open_idx" ON "entity_review_queue" USING btree ("status") WHERE "entity_review_queue"."status" = 'open';--> statement-breakpoint
CREATE UNIQUE INDEX "external_ids_current_idx" ON "external_ids" USING btree ("source_id","entity_type","external_id") WHERE "external_ids"."superseded_at" IS NULL;--> statement-breakpoint
CREATE INDEX "external_ids_internal_idx" ON "external_ids" USING btree ("entity_type","internal_id");--> statement-breakpoint
CREATE INDEX "external_ids_known_at_idx" ON "external_ids" USING btree ("known_at");